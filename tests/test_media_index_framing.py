"""Where the subject is, and where to put a crop window so it stays in shot."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import framing as F  # noqa: E402

W, H = 128, 72
NINE_SIXTEEN_OF_16_9 = (9 / 16) / (16 / 9)           # the share of a 16:9 picture a 9:16 window covers


def scene(cx, cy=0.5, r=0.08, noise=18, fill=235, seed=0, bg=110):
    rng = np.random.default_rng(seed)
    img = rng.normal(bg, noise, (H, W))
    yy, xx = np.mgrid[0:H, 0:W]
    blob = ((xx / W - cx) ** 2 * (W / H) ** 2 + (yy / H - cy) ** 2) < r ** 2
    img[blob] = fill
    return np.clip(img, 0, 255).astype(np.uint8)


# ============================ the map ============================
@pytest.mark.parametrize("cx", [0.15, 0.3, 0.5, 0.7, 0.85])
def test_a_bright_object_on_a_textured_background_is_found_and_the_window_holds_it(cx):
    sal = F.saliency(scene(cx))
    sub, win = F.subject_of(sal), F.best_window(sal, NINE_SIXTEEN_OF_16_9)
    assert sub["x"] == pytest.approx(cx, abs=0.04)
    assert abs(win["center"] - cx) <= NINE_SIXTEEN_OF_16_9 / 2, "the window contains the object"
    assert win["confidence"] > 0.5 and sal.sum() == pytest.approx(1.0)


def test_a_dark_object_and_a_soft_one_are_found_too():
    dark = F.best_window(F.saliency(255 - scene(0.7)), NINE_SIXTEEN_OF_16_9)
    assert abs(dark["center"] - 0.7) <= NINE_SIXTEEN_OF_16_9 / 2 and dark["confidence"] > 0.5
    grad = np.tile(np.linspace(60, 140, W), (H, 1))
    yy, xx = np.mgrid[0:H, 0:W]
    grad[((xx - 38) ** 2 / 14 ** 2 + (yy - 36) ** 2 / 20 ** 2) < 1] = 165
    soft = F.best_window(F.saliency(grad.astype(np.uint8)), NINE_SIXEL if False else NINE_SIXTEEN_OF_16_9)
    assert abs(soft["center"] - 0.30) <= 0.12 and soft["confidence"] > 0.4


@pytest.mark.parametrize("name,img", [("flat", np.full((H, W), 90, np.uint8)),
                                       ("fine texture", np.clip(np.random.default_rng(1).normal(110, 18, (H, W)), 0, 255).astype(np.uint8)),
                                       ("stronger texture", np.clip(np.random.default_rng(2).normal(110, 8, (H, W)), 0, 255).astype(np.uint8))])
def test_a_picture_with_no_subject_has_no_confidence(name, img):
    assert F.best_window(F.saliency(img), NINE_SIXTEEN_OF_16_9)["confidence"] < F.LOW_CONFIDENCE, name


def test_a_flat_picture_centres_the_window_because_nothing_pulls_it_elsewhere():
    assert F.best_window(F.saliency(np.full((H, W), 90, np.uint8)), 0.3)["center"] == pytest.approx(0.5, abs=0.06)


def test_the_bigger_object_wins_over_a_smaller_one():
    both = np.maximum(scene(0.2, r=0.1), scene(0.8, r=0.04))
    assert F.best_window(F.saliency(both), NINE_SIXTEEN_OF_16_9)["center"] < 0.4


def test_the_window_is_a_share_of_the_picture_and_its_edges_stay_inside():
    sal = F.saliency(scene(0.97))
    win = F.best_window(sal, 0.4)
    assert 0.2 <= win["center"] <= 0.8, "a window cannot hang over the edge of the picture"
    tall = F.best_window(F.saliency(scene(0.5, cy=0.2, r=0.1)), 0.4, axis="y")
    assert tall["center"] < 0.5


def test_a_face_outweighs_a_brighter_thing_elsewhere():
    sal = F.saliency(scene(0.8, r=0.06))
    assert F.best_window(sal, NINE_SIXTEEN_OF_16_9)["center"] > 0.6
    boosted = F.with_faces(sal, [(0.1, 0.3, 0.14, 0.3)])
    assert boosted.sum() == pytest.approx(1.0) and F.best_window(boosted, NINE_SIXTEEN_OF_16_9)["center"] < 0.4
    assert F.with_faces(sal, None) is sal and F.with_faces(sal, []) is sal


# ============================ over time ============================
def fake_frames(positions):
    def frames(path, start, end, samples):
        step = (end - start) / len(positions)
        return [(round(start + step * (i + 0.5), 3), scene(cx, seed=i)) for i, cx in enumerate(positions)]
    return frames


def test_a_still_subject_gives_one_position_and_does_not_move():
    out = F.framing_of("x", 0.0, 10.0, NINE_SIXTEEN_OF_16_9, frames=fake_frames([0.3] * 5))
    assert out["center"] == pytest.approx(0.3, abs=0.12) and out["moves"] is False and out["sure"] is True and out["method"] == "saliency" and out["kind"] == "measured"
    assert len(out["samples"]) == 5 and out["axis"] == "x"


def test_a_subject_that_crosses_the_frame_is_moving():
    out = F.framing_of("x", 0.0, 10.0, NINE_SIXTEEN_OF_16_9, frames=fake_frames([0.2, 0.35, 0.5, 0.65, 0.8]))
    assert out["moves"] is True and out["range"][0] < 0.35 and out["range"][1] > 0.65


def test_a_shot_with_no_subject_says_it_is_not_sure():
    out = F.framing_of("x", 0.0, 4.0, NINE_SIXTEEN_OF_16_9, frames=lambda p, a, b, n: [(0.5 * i, np.full((H, W), 90, np.uint8)) for i in range(5)])
    assert out["sure"] is False and out["confidence"] < F.LOW_CONFIDENCE


def test_faces_given_for_a_time_are_used_at_that_time():
    out = F.framing_of("x", 0.0, 10.0, NINE_SIXTEEN_OF_16_9, frames=fake_frames([0.8] * 5), faces_at=lambda t: [(0.05, 0.3, 0.15, 0.3)])
    assert out["center"] < 0.4


# ============================ where to put the clip ============================
def test_a_landscape_clip_in_a_vertical_frame_slides_by_the_subjects_distance_from_the_middle():
    fr = {"center": 0.3, "moves": False, "samples": []}
    out = F.offsets(fr, 16 / 9, 9 / 16)
    k = (16 / 9) / (9 / 16)
    assert out["property"] == "location_x" and out["factor"] == pytest.approx(k, abs=1e-3) and out["static"] == pytest.approx((0.5 - 0.3) * k, abs=1e-3)
    assert out["limit"] == pytest.approx((k - 1) / 2, abs=1e-3) and out["clamped"] is False and "keyframes" not in out


def test_the_slide_never_goes_far_enough_to_show_a_bar():
    out = F.offsets({"center": 0.02, "moves": False, "samples": []}, 16 / 9, 9 / 16)
    assert out["static"] == pytest.approx(out["limit"], abs=1e-3) and out["clamped"] is True
    assert F.offsets({"center": 0.98, "moves": False, "samples": []}, 16 / 9, 9 / 16)["static"] == pytest.approx(-out["limit"], abs=1e-3)


def test_a_centred_subject_needs_no_slide():
    assert F.offsets({"center": 0.5, "moves": False, "samples": []}, 16 / 9, 9 / 16)["static"] == 0.0


def test_a_moving_subject_gets_keyframes_at_the_times_it_was_seen():
    fr = F.framing_of("x", 2.0, 12.0, NINE_SIXTEEN_OF_16_9, frames=fake_frames([0.2, 0.35, 0.5, 0.65, 0.8]))
    out = F.offsets(fr, 16 / 9, 9 / 16)
    values = [k["value"] for k in out["keyframes"]]
    assert [k["t"] for k in out["keyframes"]] == [r["t"] for r in fr["samples"]] and values == sorted(values, reverse=True), "as the subject moves right the picture moves left"


def test_a_tall_clip_in_a_wide_frame_slides_up_or_down():
    out = F.offsets({"center": 0.3, "moves": False, "samples": []}, 9 / 16, 16 / 9)
    assert out["property"] == "location_y" and out["static"] > 0, "a subject high up: the picture moves down"


def test_the_same_shape_needs_no_reframing():
    assert F.offsets({"center": 0.3, "moves": False, "samples": []}, 16 / 9, 16 / 9 * 1.01)["property"] is None


# ============================ the tools ============================
def call(name, **kw):
    out = REGISTRY[name].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def library(monkeypatch, tmp_path):
    from classes.editor_tools import media_index_tools_precision as P
    from classes.media_index.store import Shelf
    shelf = Shelf(str(tmp_path / "shelf"))
    f = SimpleNamespace(id="V1", data={"name": "wide.mp4", "path": "/m/wide.mp4", "media_type": "video", "fingerprint": {"sha256": "a" * 64}})
    song = SimpleNamespace(id="S1", data={"name": "song.wav", "path": "/m/song.wav", "media_type": "audio", "fingerprint": None})
    monkeypatch.setattr(P, "default_shelf", lambda: shelf)
    monkeypatch.setattr(P, "resolve_files", lambda ids=None, query="": [x for x in (f, song) if x.id in (ids or [])])
    monkeypatch.setattr(P, "probe_media", lambda path: {"ok": True, "video": {"width": 1920, "height": 1080}})
    monkeypatch.setattr(P, "_project_facts", lambda: {"width": 1080, "height": 1920, "fps": 30.0})
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    calls = []

    def frames(path, start, end, samples):
        calls.append((start, end))
        return fake_frames([0.25] * samples)(path, start, end, samples)

    monkeypatch.setattr(F, "source_frames", frames)
    return SimpleNamespace(file=f, shelf=shelf, calls=calls)


def test_the_tool_says_where_to_put_the_window_and_gives_values_to_apply(library):
    head, r = call("get_framing_tool", file_ids=["V1"], start_seconds=1.0, end_seconds=6.0, aspect="9:16")
    assert r["placement"]["property"] == "location_x" and r["placement"]["static"] > 0 and r["changed"] is False
    assert r["suggestion"]["properties"] == {"scale": "Crop", "gravity": "Center", "location_x": r["placement"]["static"]}
    assert "confidence" in head and "across the picture" in head and r["frame_aspect"] == pytest.approx(0.5625, abs=1e-3) and r["framing"]["cached"] is False


def test_asking_again_is_answered_from_the_shelf(library):
    call("get_framing_tool", file_ids=["V1"], start_seconds=1.0, end_seconds=6.0)
    before = len(library.calls)
    _, again = call("get_framing_tool", file_ids=["V1"], start_seconds=1.0, end_seconds=6.0)
    assert len(library.calls) == before and again["framing"]["cached"] is True


def test_the_project_shape_is_the_default_and_a_matching_shape_needs_nothing(library):
    _, r = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="project")
    assert r["frame_aspect"] == pytest.approx(1080 / 1920, abs=1e-3)
    head, same = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="16:9")
    assert "nothing to reframe" in head and "framing" not in same


def test_a_moving_subject_adds_keyframes_to_the_suggestion(library, monkeypatch):
    monkeypatch.setattr(F, "source_frames", lambda p, a, b, n: fake_frames([0.2, 0.35, 0.5, 0.65, 0.8])(p, a, b, n))
    head, r = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=10.0, aspect="9:16")
    assert r["framing"]["moves"] is True and len(r["suggestion"]["keyframes"]["location_x"]) == 5 and "the subject moves" in head


@pytest.mark.parametrize("kw,fragment", [(dict(file_ids=["V1"], start_seconds=1.0, end_seconds=1.1), "0.2 s"), (dict(file_ids=["S1"], start_seconds=0.0, end_seconds=4.0), "only pictures"),
                                         (dict(file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="banana"), "aspect must be"),
                                         (dict(file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="50:1"), "outside")])
def test_what_it_cannot_do_is_refused(library, kw, fragment):
    assert fragment in REGISTRY["get_framing_tool"].func(**kw)


# ============================ reframing a real project, with real undo ============================
@pytest.fixture
def project(editor, tmp_path, monkeypatch):
    from classes.editor_tools import media_index_tools_precision as P
    from classes.media_index.store import Shelf
    shelf = Shelf(str(tmp_path / "shelf"))
    monkeypatch.setattr(P, "default_shelf", lambda: shelf)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    editor.store._data["width"], editor.store._data["height"] = 1080, 1920
    video = editor.add_file("video", duration=20.0, fingerprint={"sha256": "e" * 64})
    positions = {"calls": []}

    def frames(path, start, end, samples):
        positions["calls"].append((round(start, 2), round(end, 2)))
        return fake_frames(positions.get("at", [0.25] * samples))(path, start, end, samples)

    monkeypatch.setattr(F, "source_frames", frames)
    return SimpleNamespace(ed=editor, video=video, positions=positions, shelf=shelf)


def reframe(project, **kw):
    return project.ed.call_receipt("reframe_to_subject_tool", **kw)


def test_a_landscape_clip_is_filled_and_positioned_on_the_subject_in_one_undo_step(project):
    ed = project.ed
    clip = ed.add_clip(project.video, position=0.0, layer=1000000, start=2.0, end=9.0)
    ed.mark()
    r = reframe(project)
    assert r["status"] == "applied", r
    row = r["data"]["clips"][0]
    assert row["property"] == "location_x" and row["value"] > 0 and row["moving"] is False and row["sure"] is True
    saved = ed.clip(clip)
    assert saved["scale"] == 0 and saved["gravity"] == 4
    points = saved["location_x"]["Points"]
    assert len(points) == 1 and points[0]["co"]["Y"] == pytest.approx(row["value"], abs=1e-4)
    assert project.positions["calls"][0] == (2.0, 9.0), "it looked at the clip's own trimmed range, not the whole file"
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert ed.clip(clip).get("scale") != 0 or ed.clip(clip)["location_x"]["Points"][0]["co"]["Y"] == 0.0


def test_a_moving_subject_gets_a_keyframed_position(project):
    ed = project.ed
    project.positions["at"] = [0.2, 0.35, 0.5, 0.65, 0.8]
    clip = ed.add_clip(project.video, position=0.0, layer=1000000, start=0.0, end=10.0)
    r = reframe(project)
    row = r["data"]["clips"][0]
    pts = ed.clip(clip)["location_x"]["Points"]
    assert row["moving"] is True and row["keyframes"] == 5 and len(pts) == 5
    ys = [p["co"]["Y"] for p in pts]
    assert ys == sorted(ys, reverse=True) and [p["co"]["X"] for p in pts] == sorted(p["co"]["X"] for p in pts)


def test_a_dry_run_changes_nothing(project):
    ed = project.ed
    clip = ed.add_clip(project.video, position=0.0, layer=1000000, start=0.0, end=10.0)
    ed.mark()
    r = reframe(project, dry_run=True)
    assert r["data"]["dry_run"] is True and r["data"]["clips"][0]["property"] == "location_x" and ed.undo_steps_since_mark() == 0
    assert ed.clip(clip).get("scale") != 0


def test_clips_that_must_be_left_alone_are_skipped_with_the_reason(project):
    ed = project.ed
    ed.lock_track(2000000)
    pip = ed.add_clip(project.video, position=0.0, layer=1000000, start=0.0, end=5.0, scale_x={"Points": [{"co": {"X": 1.0, "Y": 0.4}, "interpolation": 2}]})
    locked = ed.add_clip(project.video, position=0.0, layer=2000000, start=0.0, end=5.0)
    ok_clip = ed.add_clip(project.video, position=6.0, layer=1000000, start=0.0, end=5.0)
    r = reframe(project)
    reasons = {s["timeline_clip_id"]: s["reason"] for s in r["data"]["skipped"]}
    assert "picture-in-picture" in reasons[pip] and reasons[locked] == "track is locked"
    assert [c["timeline_clip_id"] for c in r["data"]["clips"]] == [ok_clip]


def test_a_clip_that_already_has_the_shape_and_audio_are_not_touched(project):
    ed = project.ed
    ed.store._data["width"], ed.store._data["height"] = 1920, 1080
    ed.add_clip(project.video, position=0.0, layer=1000000, start=0.0, end=5.0)
    assert reframe(project)["status"] in ("unchanged", "applied")
    assert "Nothing to reframe" in reframe(project)["summary"]


def test_a_shot_with_no_subject_is_still_centred_and_the_receipt_says_check_it(project, monkeypatch):
    monkeypatch.setattr(F, "source_frames", lambda p, a, b, n: [(0.5 * i, np.full((H, W), 90, np.uint8)) for i in range(5)])
    ed = project.ed
    clip = ed.add_clip(project.video, position=0.0, layer=1000000, start=0.0, end=5.0)
    r = reframe(project)
    assert r["data"]["clips"][0]["sure"] is False and "check them by eye" in r["summary"]
    assert abs(ed.clip(clip)["location_x"]["Points"][0]["co"]["Y"]) < 0.4


def test_only_the_named_clips_are_reframed(project):
    ed = project.ed
    a = ed.add_clip(project.video, position=0.0, layer=1000000, start=0.0, end=5.0)
    b = ed.add_clip(project.video, position=6.0, layer=1000000, start=0.0, end=5.0)
    r = reframe(project, timeline_clip_ids=[b])
    assert [c["timeline_clip_id"] for c in r["data"]["clips"]] == [b] and ed.clip(a).get("scale") != 0


# ============================ faces from the people scan ============================
def tracks_at(*boxes_by_time):
    return [{"samples": [{"t": t, "box": box} for t, box in boxes_by_time]}]


def test_faces_come_from_a_scan_near_the_time_asked_and_none_when_there_are_none_near():
    at = F.faces_at_from_tracks(tracks_at((2.0, [0.1, 0.2, 0.1, 0.2]), (4.0, [0.7, 0.2, 0.1, 0.2])))
    assert at(2.3) == [[0.1, 0.2, 0.1, 0.2]] and at(3.9) == [[0.7, 0.2, 0.1, 0.2]]
    assert at(10.0) is None and F.faces_at_from_tracks([])(1.0) is None
    both = F.faces_at_from_tracks(tracks_at((2.0, [0.1, 0.2, 0.1, 0.2]), (2.1, [0.7, 0.2, 0.1, 0.2])))
    assert len(both(2.05)) == 2, "two faces seen at about the same time both count"


def test_the_method_says_faces_were_used_only_when_a_face_was_there():
    plain = F.framing_of("x", 0.0, 10.0, NINE_SIXTEEN_OF_16_9, frames=fake_frames([0.8] * 5))
    assert plain["method"] == "saliency" and plain["faces_in_samples"] == 0
    faced = F.framing_of("x", 0.0, 10.0, NINE_SIXTEEN_OF_16_9, frames=fake_frames([0.8] * 5), faces_at=lambda t: [(0.05, 0.3, 0.15, 0.3)])
    assert faced["method"] == "saliency+faces" and faced["faces_in_samples"] == 5
    none_near = F.framing_of("x", 0.0, 10.0, NINE_SIXTEEN_OF_16_9, frames=fake_frames([0.8] * 5), faces_at=lambda t: None)
    assert none_near["method"] == "saliency"


@pytest.fixture
def people_on(library, monkeypatch):
    from classes import info
    from classes.media_index import people as pp
    monkeypatch.setattr(info, "USER_PATH", str(library.shelf.root) + "_user", raising=False)
    monkeypatch.setattr("classes.media_index.flags.people_enabled", lambda: True)
    sha = "a" * 64
    v = np.zeros(128, np.float32)
    v[0] = 1.0
    w = np.zeros(128, np.float32)
    w[1] = 1.0
    shots = [{"id": 0, "start": 0.0, "end": 10.0}]
    d = lambda t, vec, box: {"t": t, "box": box, "px": 100, "score": 0.9, "vec": vec}   # noqa: E731
    scan = pp.build_scan([d(t, v, [0.05, 0.3, 0.15, 0.3]) for t in (1.0, 3.0, 5.0)] + [d(t, w, [0.8, 0.3, 0.15, 0.3]) for t in (1.0, 3.0, 5.0)], shots, 6, 10.0)
    pp._write_json(pp._scan_path(sha), scan)
    pp.assign_new_faces(sha, scan)
    pp.name_person("P1", "Left")
    pp.name_person("P2", "Right")
    return pp


def test_with_people_on_the_window_follows_the_faces_and_a_named_person_is_followed_exactly(people_on, library):
    _, plain = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=8.0, aspect="9:16")
    assert plain["framing"]["method"] == "saliency+faces"
    _, left = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=8.0, aspect="9:16", person="left")
    _, right = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=8.0, aspect="9:16", person="P2")
    assert left["framing"]["center"] < 0.4 < 0.6 < right["framing"]["center"], "the window goes to the person asked for"
    assert left["framing"]["person_in_shot"] is True and left["framing"]["method"] == "saliency+faces"


def test_a_person_who_is_not_in_the_stretch_says_so_and_a_missing_name_or_scan_is_an_error(people_on, library):
    head, r = call("get_framing_tool", file_ids=["V1"], start_seconds=8.5, end_seconds=9.9, aspect="9:16", person="Left")
    assert "was not found on screen" in head and r["framing"]["person_in_shot"] is False
    assert "no person 'Zed'" in REGISTRY["get_framing_tool"].func(file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="9:16", person="Zed")
    people_on.delete_all()
    assert "no people scan" in REGISTRY["get_framing_tool"].func(file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="9:16", person="Left")
    _, still = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="9:16")
    assert still["framing"]["method"] == "saliency", "without a scan, framing is as before"


def test_following_a_person_needs_the_preference(library, monkeypatch):
    monkeypatch.setattr("classes.media_index.flags.people_enabled", lambda: False)
    assert "needs the people preference" in REGISTRY["get_framing_tool"].func(file_ids=["V1"], start_seconds=0.0, end_seconds=4.0, aspect="9:16", person="Left")


def test_a_cached_framing_is_not_reused_for_a_different_person(people_on, library):
    call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=8.0, aspect="9:16", person="Left")
    _, other = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=8.0, aspect="9:16", person="Right")
    assert other["framing"]["cached"] is False
    _, again = call("get_framing_tool", file_ids=["V1"], start_seconds=0.0, end_seconds=8.0, aspect="9:16", person="Right")
    assert again["framing"]["cached"] is True
