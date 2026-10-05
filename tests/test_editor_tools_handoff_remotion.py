"""The Remotion editor tools through execute_tool: list, import (linked + native, one undo step), export."""

import copy
import json
import os

import pytest

from classes.handoff import alpha, jobs
from classes.handoff import linked_media as lm
from classes.handoff.remotion import helper
from classes.handoff.remotion.provider import RemotionProvider
from handoff_fakes import linked, tt  # noqa: F401  (fixtures)
from remotion_fakes import FakeHelper, make_project
from test_handoff_remotion_export import build_project


@pytest.fixture
def remotion(linked, tmp_path, monkeypatch):  # noqa: F811
    """The linked editor + the Remotion provider + a FakeHelper instead of Node; TitleCard is transparent."""
    fake = FakeHelper()
    monkeypatch.setattr(helper, "run_helper", fake)
    monkeypatch.setattr(helper, "prune_bundles", lambda *a, **k: [])
    monkeypatch.setattr(alpha, "has_transparency", lambda p: _comp_of(fake, p) == "TitleCard")
    lm.register_provider(RemotionProvider())
    linked.fake = fake
    linked.project_dir = make_project(str(tmp_path / "promo"))
    video = linked.add_file("video")
    linked.add_clip(video, position=0.0, layer=1000000)
    return linked


def _comp_of(fake, still_path):
    for call in reversed(fake.calls):
        if call["command"] == "still" and still_path.startswith(call["options"]["out_dir"]):
            return call["options"]["composition"]
    return None


def _data(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


def _renders(editor):
    folder = os.path.join(editor.user_path, "links", "remotion")
    return sorted(n for n in os.listdir(folder)) if os.path.isdir(folder) else []


# ---------------------------------------------------------------------------
# list_remotion_compositions_tool
# ---------------------------------------------------------------------------

def test_list_reports_sizes_props_and_code_locations(remotion):
    data = _data(remotion.call("list_remotion_compositions_tool", project_dir=remotion.project_dir))
    comps = {c["id"]: c for c in data["compositions"]}
    assert set(comps) == {"TitleCard", "Scene"} and not data["static_only"]
    title = comps["TitleCard"]
    assert (title["width"], title["height"], title["fps"], title["duration_frames"], title["duration"]) == \
        (1920, 1080, 30.0, 90, 3.0)
    assert title["default_props"]["title"] == "Hello Zenvi" and title["folder"] == "Graphics"
    assert (title["file"], title["line"]) == ("src/TitleCard.tsx", 3)
    assert data["project"]["remotion_version"] == "4.0.532" and data["project"]["installed"]
    assert remotion.undo_steps_since_mark() == 0


def test_list_without_node_modules_reads_the_code_and_says_how_to_install(remotion, tmp_path):
    bare = make_project(str(tmp_path / "bare"), installed=False)
    data = _data(remotion.call("list_remotion_compositions_tool", project_dir=bare))
    assert data["static_only"] and {c["id"] for c in data["compositions"]} == {"TitleCard", "Scene"}
    assert data["compositions"][0]["width"] is None and "npm install" in data["warnings"][0]
    assert remotion.fake.calls == []


def test_list_refuses_folders_that_are_not_remotion(remotion, tmp_path):
    (tmp_path / "plain").mkdir()
    out = remotion.call("list_remotion_compositions_tool", project_dir=str(tmp_path / "plain"))
    assert out.startswith("Error") and "no package.json" in out


# ---------------------------------------------------------------------------
# import_remotion_project_tool
# ---------------------------------------------------------------------------

def test_import_renders_linked_clips_in_one_undo_step_with_titles_on_top(remotion):
    receipt = remotion.call_receipt("import_remotion_project_tool", project_dir=remotion.project_dir,
                                    props={"TitleCard": {"title": "Launch day"}})
    assert receipt["status"] == "applied", receipt["summary"]
    data = receipt["data"]
    assert remotion.undo_steps_since_mark() == 1
    linked_clips = {c["composition"]: c for c in data["linked"]}
    assert linked_clips["TitleCard"]["codec"] == "prores4444" and linked_clips["Scene"]["codec"] == "h264"
    assert linked_clips["Scene"]["layer"] < linked_clips["TitleCard"]["layer"]  # opaque below transparent
    assert linked_clips["TitleCard"]["props"] == {"title": "Launch day", "subtitle": "From Remotion",
                                                 "accent": "#FF5A36"}
    title_file = remotion.file(linked_clips["TitleCard"]["file_id"])
    link = lm.read_link(title_file)
    assert link["kind"] == "remotion" and link["source"]["composition"] == "TitleCard"
    assert link["source"]["file"] == "src/TitleCard.tsx" and link["source"]["line"] == 3
    assert link["render"]["codec"] == "prores4444" and link["remotion"]["codec"] == "auto"
    assert lm.check_link(title_file).state == "fresh"
    renders = [c for c in remotion.fake.calls if c["command"] == "render"]
    assert [r["props"]["title"] for r in renders if r["options"]["composition"] == "TitleCard"] == ["Launch day"]
    assert len(_renders(remotion)) == 2
    remotion.undo()
    assert remotion.file(linked_clips["TitleCard"]["file_id"]) is None
    assert len(remotion.clips()) == 1


def test_bad_requests_are_refused_before_anything_renders(remotion):
    out = remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir, compositions=["Nope"])
    assert out.startswith("Error") and "has no composition 'Nope'" in out and "Scene, TitleCard" in out
    out = remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir, track="2")
    assert out.startswith("Error") and "only be given when importing one composition" in out
    out = remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir,
                        compositions=["Scene"], track="99")
    assert out.startswith("Error")
    out = remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir,
                        compositions=["Scene"], props={"TitleCard": {"title": "x"}})
    assert out.startswith("Error") and "not being imported" in out
    out = remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir, codec="webm")
    assert out.startswith("Error")
    assert "still" not in remotion.fake.commands() and "render" not in remotion.fake.commands()
    assert remotion.undo_steps_since_mark() == 0 and _renders(remotion) == []


def test_a_failed_or_cancelled_render_adds_nothing_and_cleans_up(remotion):
    calls = []

    def fail_second(command, opts):
        if command == "render":
            calls.append(opts["composition"])
            if len(calls) == 2:
                raise helper.HelperError("the browser crashed", "FAILED")

    remotion.fake.during = fail_second
    out = remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir)
    assert out.startswith("Error") and "the browser crashed" in out
    assert remotion.undo_steps_since_mark() == 0 and len(remotion.clips()) == 1
    assert _renders(remotion) == []  # the first, finished render was deleted
    remotion.fake.during = None
    remotion.fake.fail, remotion.fake.fail_on = jobs.JobCancelled("cancelled"), "render"
    out = remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir, compositions=["Scene"])
    assert out.startswith("Error") and "cancelled; nothing changed" in out
    assert remotion.undo_steps_since_mark() == 0 and _renders(remotion) == []


def test_import_of_an_uninstalled_project_says_how_to_install(remotion, tmp_path):
    bare = make_project(str(tmp_path / "bare"), installed=False)
    out = remotion.call("import_remotion_project_tool", project_dir=bare)
    assert out.startswith("Error") and "npm install" in out


def test_explicit_codec_and_track_for_one_composition(remotion):
    remotion.add_track(6000000, label="Overlays")
    data = _data(remotion.call("import_remotion_project_tool", project_dir=remotion.project_dir,
                               compositions=["TitleCard"], codec="h264", track="Overlays", position=2.0))
    (clip,) = data["linked"]
    assert clip["codec"] == "h264" and clip["layer"] == 6000000 and clip["position"] == 2.0


# ---------------------------------------------------------------------------
# export_to_remotion_tool and the round trip
# ---------------------------------------------------------------------------

def test_export_then_import_restores_the_native_clips(remotion, tmp_path):
    remotion.store._data.update(clips=[], files=[])
    build_project(remotion, str(tmp_path / "media"))
    original = copy.deepcopy(remotion.store._data)
    out_dir = str(tmp_path / "trip-remotion")
    data = _data(remotion.call("export_to_remotion_tool", output_dir=out_dir))
    assert data["clips"] == 4 and os.path.isfile(os.path.join(out_dir, "src", "zenvi", "timeline.json"))
    assert remotion.undo_steps_since_mark() == 0
    # back in an empty timeline: the same clips, files, transition and marker
    remotion.store._data.update(clips=[], files=[], effects=[], markers=[])
    remotion.mark()
    listing = _data(remotion.call("list_remotion_compositions_tool", project_dir=out_dir))
    assert listing["project"]["zenvi_generated"]
    receipt = remotion.call_receipt("import_remotion_project_tool", project_dir=out_dir, position=0.0)
    assert receipt["status"] == "applied", receipt["summary"]
    assert receipt["data"]["native"]["clips"] and receipt["data"]["linked"] == []
    assert remotion.undo_steps_since_mark() == 1
    for key in ("files", "clips", "effects", "markers"):
        assert sorted(remotion.get(key), key=lambda d: d["id"]) == sorted(original[key], key=lambda d: d["id"]), key
    assert "render" not in remotion.fake.commands()  # nothing was rendered for the native restore


def test_export_refuses_a_foreign_folder(remotion, tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("x")
    out = remotion.call("export_to_remotion_tool", output_dir=str(busy))
    assert out.startswith("Error") and "not empty" in out
    assert remotion.call("export_to_remotion_tool", output_dir="").startswith("Error")
