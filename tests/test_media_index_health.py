"""Media health: what will cause trouble in an edit, and the probe facts it reads."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import health as H, probe  # noqa: E402
from classes.media_index.facts import technical_of  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

PROJECT = {"width": 1920, "height": 1080, "fps": 30.0}


def tech(**video):
    base = {"codec": "h264", "width": 1920, "height": 1080, "fps": 30.0, "nominal_fps": 30.0, "vfr": False, "interlaced": False, "bit_depth": 8,
            "hdr": False, "color_transfer": "bt709", "orientation": "landscape"}
    return {"duration": 12.0, "video": {**base, **video}, "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2}}


def codes(t, project=PROJECT):
    return [i["code"] for i in H.file_issues(t, project)]


# ============================ the probe facts ============================
def ffprobe_json(**over):
    video = {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001", "r_frame_rate": "30000/1001",
             "pix_fmt": "yuv420p", **over}
    return {"streams": [video, {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2, "channel_layout": "stereo"}],
            "format": {"duration": "12.0", "format_name": "mov,mp4", "bit_rate": "8000000",
                       "tags": {"com.apple.quicktime.make": "Apple", "com.apple.quicktime.model": "iPhone 15 Pro", "com.apple.quicktime.software": "17.4"}}}


def test_a_steady_clip_is_not_variable_frame_rate():
    v = probe.parse_probe(ffprobe_json())["video"]
    assert v["vfr"] is False and v["interlaced"] is False and v["nominal_fps"] == pytest.approx(29.97, abs=0.01)


def test_a_phone_clip_whose_average_rate_differs_from_its_nominal_rate_is_variable():
    v = probe.parse_probe(ffprobe_json(avg_frame_rate="27300/1000", r_frame_rate="30/1"))["video"]
    assert v["vfr"] is True and v["fps"] == pytest.approx(27.3) and v["nominal_fps"] == 30.0


@pytest.mark.parametrize("order,expected", [("tt", True), ("bb", True), ("tb", True), ("bt", True), ("progressive", False), ("", False), ("unknown", False)])
def test_field_order_says_whether_it_is_interlaced(order, expected):
    assert probe.parse_probe(ffprobe_json(field_order=order))["video"]["interlaced"] is expected


def test_camera_make_model_and_software_come_from_the_tags():
    out = probe.parse_probe(ffprobe_json())
    assert out["camera"] == {"make": "Apple", "model": "iPhone 15 Pro", "software": "17.4"} and out["bit_rate"] == 8000000
    assert out["audio"]["channel_layout"] == "stereo"


def test_a_file_with_no_camera_tags_has_no_camera():
    data = ffprobe_json()
    data["format"]["tags"] = {}
    assert probe.parse_probe(data)["camera"] == {}
    assert probe.parse_camera(None) == {}
    assert probe.parse_camera({"MAKE": "  Sony  ", "Model": "A7"}) == {"make": "Sony", "model": "A7"}
    assert probe.parse_camera({"encoder": "Lavf60.3.100"}) == {}, "the muxer's name is not the camera's software"


def test_the_technical_block_keeps_what_the_health_check_needs_and_no_picture_data():
    t = technical_of(probe.parse_probe(ffprobe_json(avg_frame_rate="27/1", r_frame_rate="30/1")))
    assert t["video"]["vfr"] is True and t["video"]["fps"] == 27.0 and t["audio"]["channels"] == 2 and t["duration"] == 12.0
    assert set(t["video"]) == {"codec", "width", "height", "rotation", "fps", "nominal_fps", "vfr", "interlaced", "bit_depth", "hdr",
                               "color_transfer", "bit_rate", "orientation"}
    assert technical_of({}) == {"duration": None, "format": None, "bit_rate": None, "video": None, "audio": None}


# ============================ the rules ============================
def test_a_clean_clip_in_a_matching_project_has_no_issues():
    assert H.file_issues(tech(), PROJECT) == []


@pytest.mark.parametrize("what,t,expected", [
    ("variable frame rate", tech(vfr=True, fps=27.3, nominal_fps=30.0), "variable_frame_rate"),
    ("interlaced", tech(interlaced=True), "interlaced"),
    ("HDR", tech(hdr=True, color_transfer="smpte2084"), "hdr"),
    ("a very wide picture", tech(width=2560, height=800), "odd_aspect"),
    ("heavy hevc 10-bit", tech(codec="hevc", bit_depth=10), "heavy_codec"),
    ("heavy 4K", tech(codec="hevc", width=3840, height=2160), "heavy_codec"),
])
def test_each_rule_fires_on_its_own_fault(what, t, expected):
    assert expected in codes(t), what


@pytest.mark.parametrize("fps,expected_severity", [(60.0, "info"), (15.0, "info"), (24.0, "warn"), (25.0, "warn"), (23.976, "warn")])
def test_a_frame_rate_that_divides_evenly_is_only_noted_and_one_that_does_not_is_a_warning(fps, expected_severity):
    issue = next(i for i in H.file_issues(tech(fps=fps), PROJECT) if i["code"] == "fps_mismatch")
    assert issue["severity"] == expected_severity and issue["fix"]


def test_the_same_frame_rate_up_to_ntsc_rounding_is_not_a_mismatch():
    assert H.fps_fit(29.97, 30.0) is None and H.fps_fit(30.0, 30.0) is None and H.fps_fit(0.0, 30.0) is None and H.fps_fit(30.0, 0.0) is None


def test_a_clip_much_smaller_than_the_project_is_a_warning_but_a_small_project_is_fine():
    assert "low_resolution" in codes(tech(width=640, height=360))
    assert "low_resolution" not in codes(tech(width=640, height=360), {"width": 1280, "height": 720, "fps": 30.0})
    assert "low_resolution" not in codes(tech(width=1280, height=720))


def test_a_portrait_clip_in_a_landscape_project_is_noted_and_square_is_not():
    assert "orientation_mismatch" in codes(tech(width=1080, height=1920, orientation="portrait"))
    assert "orientation_mismatch" not in codes(tech(width=1080, height=1080, orientation="square"))
    assert "orientation_mismatch" not in codes(tech(width=1080, height=1920), {"width": 1080, "height": 1920, "fps": 30.0})


def test_audio_and_length_facts():
    t = tech()
    t["audio"]["sample_rate"] = 22050
    assert "audio_sample_rate" in codes(t)
    silent = tech()
    silent["audio"] = None
    assert "no_audio" in codes(silent)
    long = tech()
    long["duration"] = 3600.0
    assert "long_file" in codes(long)


def test_the_worst_issue_comes_first_and_missing_facts_give_nothing():
    t = tech(interlaced=True, codec="hevc", bit_depth=10)
    order = [i["severity"] for i in H.file_issues(t, PROJECT)]
    assert order == sorted(order, key={"problem": 0, "warn": 1, "info": 2}.get)
    assert H.file_issues(None) == [] and H.file_issues({}) == []


def test_without_a_project_only_the_clips_own_faults_are_reported():
    assert [i["code"] for i in H.file_issues(tech(fps=24.0, width=640, height=360, vfr=True))] == ["variable_frame_rate"]


def test_the_summary_names_mixed_frame_rates_and_counts_common_issues():
    rows = [{"file_id": "a", "name": "a.mp4", "fps": 30.0, "issues": []},
            {"file_id": "b", "name": "b.mp4", "fps": 24.0, "issues": H.file_issues(tech(fps=24.0), PROJECT)},
            {"file_id": "c", "name": "c.mp4", "fps": 24.0, "issues": H.file_issues(tech(fps=24.0, interlaced=True), PROJECT)}]
    s = H.project_summary(rows, PROJECT)
    assert s["files"] == 3 and s["with_problems"] == 2 and s["common_issues"][0] == {"code": "fps_mismatch", "files": 2}
    assert s["frame_rates"] == {"24": ["b.mp4", "c.mp4"], "30": ["a.mp4"]} and "24, 30 fps in a 30 fps project" in s["mixed_frame_rates"]
    assert "mixed_frame_rates" not in H.project_summary(rows[:1], PROJECT)


# ============================ the tool ============================
class Saved(SimpleNamespace):
    pass


def call(**kw):
    out = REGISTRY["check_media_health_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def library(monkeypatch, tmp_path):
    from classes.editor_tools import media_index_tools_precision as P
    shelf = Shelf(str(tmp_path / "shelf"))

    def f(fid, name, sha, t=None, camera=None):
        if sha and t:
            shelf.set_source(sha, technical=t, camera=camera or None)
        return Saved(id=fid, data={"name": name, "path": f"/m/{name}", "media_type": "video", "fingerprint": {"sha256": sha} if sha else None})

    files = [f("A", "phone.mp4", "a" * 64, tech(vfr=True, fps=27.3, nominal_fps=30.0), {"make": "Apple", "model": "iPhone"}),
             f("B", "clean.mp4", "b" * 64, tech()), f("C", "film.mp4", "c" * 64, tech(fps=24.0, interlaced=True)),
             f("D", "fresh.mp4", None)]
    monkeypatch.setattr(P, "default_shelf", lambda: shelf)
    monkeypatch.setattr(P, "_all_files", lambda: files)
    monkeypatch.setattr(P, "resolve_files", lambda ids=None, query="": [x for x in files if x.id in (ids or [])])
    monkeypatch.setattr(P, "get_app", lambda: SimpleNamespace(project={"width": 1920, "height": 1080, "fps": {"num": 30, "den": 1}}))
    probes = []
    monkeypatch.setattr(P, "probe_media", lambda path: probes.append(path) or {"ok": True, "duration": 5.0, "has_video": True, "has_audio": True,
                                                                                "video": tech(interlaced=True)["video"], "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2}})
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    return SimpleNamespace(files=files, shelf=shelf, probes=probes)


def test_the_tool_lists_files_with_something_worth_fixing_and_a_summary(library):
    head, r = call()
    names = {x["name"]: x for x in r["files"]}
    assert set(names) == {"phone.mp4", "film.mp4", "fresh.mp4"} and "clean.mp4" not in names
    assert [i["code"] for i in names["phone.mp4"]["issues"]][0] == "variable_frame_rate" and names["phone.mp4"]["camera"] == {"make": "Apple", "model": "iPhone"}
    assert {i["code"] for i in names["film.mp4"]["issues"]} >= {"interlaced", "fps_mismatch"}
    assert head.startswith("3 of 4 file(s) have something worth fixing") and "mixed frame rates" in head and r["rollup"]["mixed_frame_rates"]
    assert r["changed"] is False


def test_a_file_indexed_before_facts_were_kept_is_probed_on_the_spot_and_known_ones_are_not(library):
    call()
    assert library.probes == ["/m/fresh.mp4"], "only the file with no saved technical facts was probed"


def test_only_problems_false_lists_every_file_and_a_target_limits_it(library):
    _, all_files = call(only_problems=False)
    assert {x["name"] for x in all_files["files"]} == {"phone.mp4", "clean.mp4", "film.mp4", "fresh.mp4"}
    _, one = call(file_ids=["B"], only_problems=False)
    assert [x["name"] for x in one["files"]] == ["clean.mp4"] and one["files"][0]["issues"] == []


def test_nothing_wrong_says_so(library):
    head, r = call(file_ids=["B"])
    assert head == "no problems found in 1 file(s)" and r["files"] == []
