"""Edits made to an exported timeline.json come back onto the original Zenvi objects (classes.handoff.remotion.edits)."""

import copy
import json
import os
import time

import pytest

from classes.handoff.keyframes import BEZIER, CONSTANT, LINEAR
from classes.handoff.remotion import exporter, restore
from handoff_fakes import linked, tt  # noqa: F401  (fixtures)
from test_handoff_remotion_export import _export, build_project


@pytest.fixture
def exported(linked, tmp_path):  # noqa: F811
    """(editor, export folder, original project data, clip ids, file ids)."""
    clips, files = build_project(linked, str(tmp_path / "media"))
    original = copy.deepcopy(linked.store._data)
    out = tmp_path / "trip-remotion"
    _export(linked, out)
    return linked, str(out), original, clips, files


def _load(out):
    return restore.load_timeline(restore.timeline_path(out))


def _save(out, timeline):
    with open(restore.timeline_path(out), "w") as fh:
        json.dump(timeline, fh)


def _restore(out):
    return restore.restored_project(_load(out), out)


def _clip(project, cid):
    return next(c for c in project["clips"] if c["id"] == cid)


def _entry(timeline, cid):
    return next(c for c in timeline["clips"] if c["id"] == cid)


def _xy(kf):
    return [(p["co"]["X"], p["co"]["Y"]) for p in kf["Points"]]


# ---------------------------------------------------------------------------
# Untouched and simple edits
# ---------------------------------------------------------------------------

def test_an_untouched_export_restores_bit_identical(exported):
    _editor, out, original, _clips, _files = exported
    project, warnings, applied = _restore(out)
    expected = exporter.lossless_project(original)
    assert json.dumps(project, sort_keys=True) == json.dumps(json.loads(json.dumps(expected)), sort_keys=True)
    assert warnings == [] and applied == []
    # re-saving the JSON unchanged (an editor reformatting it) is not an edit either
    _save(out, _load(out))
    assert _restore(out)[2] == []


def test_moving_and_trimming_clips_comes_back(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    _entry(t, clips["image"])["position"] = 2.5                       # move
    _entry(t, clips["title"])["position"] = 1.5
    video = _entry(t, clips["video"])
    video["start"], video["end"] = 3.0, 7.0                           # source in / out
    _entry(t, clips["music"])["end"] = 3.0                            # shorter
    _save(out, t)
    project, warnings, applied = _restore(out)
    assert warnings == []
    assert _clip(project, clips["image"])["position"] == 2.5
    assert _clip(project, clips["title"])["position"] == pytest.approx(1.5)
    v = _clip(project, clips["video"])
    assert (v["start"], v["end"]) == (3.0, 7.0)
    assert v["alpha"] == _clip(original, clips["video"])["alpha"]     # keyframes stay on the source (Zenvi trim)
    assert _clip(project, clips["music"])["end"] == pytest.approx(3.0)
    fields = sorted((e["id"], e["field"]) for e in applied)
    assert fields == sorted([(clips["image"], "position"), (clips["title"], "position"), (clips["video"], "start"),
                             (clips["video"], "end"), (clips["music"], "end")])
    # everything else about the clips is the original
    for cid in clips.values():
        a, b = _clip(project, cid), _clip(original, cid)
        assert {k: v for k, v in a.items() if k not in ("position", "start", "end")} == \
            {k: v for k, v in b.items() if k not in ("position", "start", "end")}


def test_deleted_and_duplicated_clips(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    t["clips"] = [c for c in t["clips"] if c["id"] != clips["music"]]
    copy_of_image = copy.deepcopy(_entry(t, clips["image"]))
    copy_of_image.update(id="IMAGECOPY1", position=4.0)
    t["clips"].append(copy_of_image)
    _save(out, t)
    project, warnings, applied = _restore(out)
    ids = {c["id"] for c in project["clips"]}
    assert clips["music"] not in ids and "IMAGECOPY1" in ids and len(project["clips"]) == 4
    dup, src = _clip(project, "IMAGECOPY1"), _clip(original, clips["image"])
    assert dup["position"] == 4.0 and dup["file_id"] == src["file_id"]
    assert {k: v for k, v in dup.items() if k not in ("id", "position")} == \
        {k: v for k, v in src.items() if k not in ("id", "position")}
    assert ("clip", clips["music"], "deleted") in [(e["kind"], e["id"], e["field"]) for e in applied]
    assert ("clip", "IMAGECOPY1", "added") in [(e["kind"], e["id"], e["field"]) for e in applied]
    assert warnings == []


def test_a_duplicate_with_a_taken_id_gets_a_new_one_and_its_effects_too(exported, linked):  # noqa: F811
    editor, _out, _original, clips, _files = exported
    effect_id = editor.add_effect(clips["image"], "Saturation")
    out = os.path.join(os.path.dirname(_out), "again")
    _export(editor, out)
    t = _load(out)
    t["clips"].append(copy.deepcopy(_entry(t, clips["image"])))       # the same id twice
    _save(out, t)
    project, _warnings, applied = _restore(out)
    copies = [c for c in project["clips"] if c["file_id"] == _clip(project, clips["image"])["file_id"]]
    assert len(copies) == 2 and len({c["id"] for c in copies}) == 2
    new = next(c for c in copies if c["id"] != clips["image"])
    assert new["effects"][0]["id"] != effect_id and new["effects"][0]["class_name"] == "Saturation"


def test_keyframe_value_time_and_easing_edits(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    video = _entry(t, clips["video"])
    video["keyframes"]["alpha"][1]["value"] = 0.5                     # value
    video["keyframes"]["alpha"][1]["frame"] = 80                      # time (clip frames: X - 1)
    image = _entry(t, clips["image"])
    image["keyframes"]["location_x"][1]["easing"] = "linear"          # easing
    image["keyframes"]["rotation"] = [{"frame": 0, "value": 0, "easing": None},
                                      {"frame": 30, "value": 15, "easing": [0.16, 1, 0.3, 1]}]  # a new property
    _save(out, t)
    project, warnings, applied = _restore(out)
    assert warnings == []
    v = _clip(project, clips["video"])
    assert _xy(v["alpha"]) == [(61.0, 0.0), (81.0, 0.5)]              # frame 80 -> X 81
    first_before = _clip(original, clips["video"])["alpha"]["Points"][0]
    assert v["alpha"]["Points"][0] == first_before                    # the untouched point is verbatim
    img = _clip(project, clips["image"])
    assert img["location_x"]["Points"][1]["interpolation"] == LINEAR
    assert img["location_x"]["Points"][0] == _clip(original, clips["image"])["location_x"]["Points"][0]
    rot = img["rotation"]["Points"]
    assert _xy(img["rotation"]) == [(1.0, 0.0), (31.0, 15.0)] and rot[1]["interpolation"] == BEZIER
    assert rot[0]["handle_right"] == {"X": 0.16, "Y": 1.0} and rot[1]["handle_left"] == {"X": 0.3, "Y": 1.0}
    assert {e["field"] for e in applied} == {"keyframes.alpha", "keyframes.location_x", "keyframes.rotation"}


def test_hold_easing_removed_properties_and_trim_plus_keyframes(exported):
    _editor, out, _original, clips, _files = exported
    t = _load(out)
    image = _entry(t, clips["image"])
    del image["keyframes"]["scale_x"]                                 # removed -> the default
    image["keyframes"]["location_x"][1]["easing"] = "hold"
    video = _entry(t, clips["video"])
    video["start"] = 1.0                                              # trim AND re-key the fade-in
    video["keyframes"]["alpha"] = [{"frame": 30, "value": 0, "easing": None},  # at the new first frame
                                   {"frame": 40, "value": 1, "easing": "linear"}]
    _save(out, t)
    project, warnings, _ = _restore(out)
    img = _clip(project, clips["image"])
    assert _xy(img["scale_x"]) == [(1.0, 1.0)]
    assert img["location_x"]["Points"][1]["interpolation"] == CONSTANT
    v = _clip(project, clips["video"])
    assert v["start"] == 1.0 and _xy(v["alpha"]) == [(31.0, 0.0), (41.0, 1.0)]
    assert warnings == []


def test_track_moves_new_tracks_and_track_renames(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    _entry(t, clips["title"])["track"] = 0                            # onto the bottom track
    _entry(t, clips["image"])["layer"] = 9000000                      # a track that does not exist yet
    t["tracks"][0]["name"] = "Picture"
    t["tracks"][1]["locked"] = True
    _save(out, t)
    project, warnings, _ = _restore(out)
    assert _clip(project, clips["title"])["layer"] == t["tracks"][0]["layer"]
    assert _clip(project, clips["image"])["layer"] == 9000000
    layers = {ly["number"]: ly for ly in project["layers"]}
    assert 9000000 in layers and layers[9000000]["lock"] is False
    assert layers[t["tracks"][0]["layer"]]["label"] == "Picture" and layers[t["tracks"][1]["layer"]]["lock"] is True
    assert len(project["layers"]) == len(original["layers"]) + 1 and warnings == []


def test_titles_flags_blend_modes_and_another_file(exported):
    _editor, out, _original, clips, files = exported
    t = _load(out)
    image = _entry(t, clips["image"])
    image["title"] = "Logo card"
    image["blendMode"] = "screen"
    image["gravity"] = 4
    _entry(t, clips["video"])["hasAudio"] = False
    _entry(t, clips["title"])["fileId"] = files["image"]              # show the logo instead of the title
    _save(out, t)
    project, warnings, _ = _restore(out)
    img = _clip(project, clips["image"])
    assert (img["title"], img["composite"], img["gravity"]) == ("Logo card", 14, 4)
    assert _xy(_clip(project, clips["video"])["has_audio"]) == [(1.0, 0.0)]
    swapped = _clip(project, clips["title"])
    assert swapped["file_id"] == files["image"] and swapped["reader"]["path"].endswith("logo.png")
    assert warnings == []


# ---------------------------------------------------------------------------
# Markers and transitions
# ---------------------------------------------------------------------------

def test_marker_moves_renames_recolours_deletions_and_additions(exported, linked):  # noqa: F811
    editor, out, _original, _clips, _files = exported
    t = _load(out)
    t["markers"][0].update(time=3.5, name="Chorus", color="green")
    t["markers"].append({"name": "Outro", "frame": 150, "color": "purple"})
    _save(out, t)
    project, warnings, _ = _restore(out)
    m1 = next(m for m in project["markers"] if m["id"] == "M1")
    assert (m1["position"], m1["name"], m1["vector"], m1["icon"]) == (3.5, "Chorus", "green", "green.png")
    new = next(m for m in project["markers"] if m.get("name") == "Outro")
    assert new["position"] == 5.0 and new["vector"] == "purple" and new["id"] != "M1"
    t["markers"] = []
    _save(out, t)
    assert _restore(out)[0]["markers"] == [] and warnings == []


def test_transition_length_and_deletion(exported):
    _editor, out, original, _clips, _files = exported
    t = _load(out)
    t["transitions"][0]["durationInFrames"] = 15
    t["transitions"][0]["from"] = 40
    _save(out, t)
    project, warnings, applied = _restore(out)
    (tr,) = project["effects"]
    assert tr["position"] == pytest.approx(40 / 30) and tr["end"] == pytest.approx(0.5)
    assert [p["co"]["X"] for p in tr["brightness"]["Points"]] == [1, 16]   # refit like a drag-trim
    assert warnings == [] and {e["field"] for e in applied} == {"position", "duration"}
    t["transitions"] = []
    _save(out, t)
    assert _restore(out)[0]["effects"] == []


# ---------------------------------------------------------------------------
# Refused and unsupported edits
# ---------------------------------------------------------------------------

def test_impossible_values_are_refused_field_by_field(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    _entry(t, clips["video"])["keyframes"]["alpha"][1]["value"] = 2.0
    _entry(t, clips["video"])["end"] = 99.0                           # past the 12 s media
    _entry(t, clips["image"])["position"] = -3
    _entry(t, clips["image"])["keyframes"]["location_x"][1]["easing"] = [2, 0, 1, 1]
    _entry(t, clips["title"])["track"] = 17
    _entry(t, clips["title"])["scaleMode"] = 9
    t["markers"][0]["color"] = "chartreuse"
    _save(out, t)
    project, warnings, applied = _restore(out)
    text = "\n".join(warnings)
    for expected in ("keyframes.alpha: 2.0 is outside 0..1", "past the end of its 12 s media",
                     "position must be 0 or more seconds", "easing must be 'linear', 'hold' or [x1, y1, x2, y2]",
                     "track 17 is not one of the", "scaleMode must be 0..3", "color must be one of"):
        assert expected in text, expected
    for cid in (clips["video"], clips["image"], clips["title"]):
        assert _clip(project, cid) == _clip(original, cid)            # nothing half-applied
    assert next(m for m in project["markers"] if m["id"] == "M1")["vector"] == "red"
    assert applied == []


def test_edits_that_cannot_be_mapped_back_are_reported(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    _entry(t, clips["video"])["time"]["playbackRate"] = 2
    _entry(t, clips["image"])["filters"] = [{"fn": "blur", "unit": "px", "keys": [{"frame": 0, "value": 4,
                                                                                   "easing": None}]}]
    _entry(t, clips["image"])["wobble"] = True
    t["composition"]["fps"] = 60
    t["clips"].append({"id": "NEW", "fileId": "nope", "src": "zenvi-media/new.mp4", "from": 0,
                       "durationInFrames": 10})
    _save(out, t)
    project, warnings, _ = _restore(out)
    text = "\n".join(warnings)
    for expected in ("speed edits", "effect edits are not brought back", "'wobble' is not a field",
                     "'composition' was edited but not brought back", "new media is not brought back"):
        assert expected in text, expected
    assert project["clips"] == exporter.lossless_project(original)["clips"]


# ---------------------------------------------------------------------------
# Media edited in the Remotion project, and the import path
# ---------------------------------------------------------------------------

def test_a_title_svg_edited_in_the_remotion_project_comes_back(exported):
    _editor, out, _original, clips, files = exported
    copy_path = os.path.join(out, "public", "zenvi-media", "Title.svg")
    with open(copy_path, "w") as fh:
        fh.write("<svg><text>New title</text></svg>")
    later = time.time() + 5
    os.utime(copy_path, (later, later))
    project, warnings, _ = _restore(out)
    title_file = next(f for f in project["files"] if f["id"] == files["title"])
    assert title_file["path"] == copy_path and _clip(project, clips["title"])["reader"]["path"] == copy_path
    assert any("was edited in the Remotion project" in w for w in warnings)
    # an original edited after the copy wins
    original_path = os.path.join(os.path.dirname(out), "media", "title.svg")
    os.utime(original_path, (later + 10, later + 10))
    project, warnings, _ = _restore(out)
    assert next(f for f in project["files"] if f["id"] == files["title"])["path"] == original_path
    assert any("kept the original" in w for w in warnings)


def test_insert_native_applies_the_edits_in_one_undo_step(exported):
    editor, out, original, clips, _files = exported
    t = _load(out)
    _entry(t, clips["image"])["position"] = 3.0
    t["clips"] = [c for c in t["clips"] if c["id"] != clips["music"]]
    _save(out, t)
    editor.store._data.update(clips=[], files=[], effects=[], markers=[])
    editor.mark()
    receipt = restore.insert_native(_load(out), out, position=0.0)
    assert editor.undo_steps_since_mark() == 1
    assert {c["id"] for c in editor.get("clips")} == {clips["video"], clips["image"], clips["title"]}
    assert next(c for c in editor.get("clips") if c["id"] == clips["image"])["position"] == 3.0
    assert {(e["id"], e["field"]) for e in receipt["edits"]} == {(clips["image"], "position"),
                                                                  (clips["music"], "deleted")}
    editor.undo()
    assert editor.get("clips") == []


def test_frame_fields_are_not_a_second_timing_source(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    _entry(t, clips["image"])["from"] = 90                            # Remotion habit: frames
    _entry(t, clips["image"])["durationInFrames"] = 30
    _save(out, t)
    project, warnings, applied = _restore(out)
    assert _clip(project, clips["image"]) == _clip(original, clips["image"]) and applied == []
    text = "\n".join(warnings)
    assert "'from' was edited but not brought back: move the clip with 'position'" in text
    assert "'durationInFrames' was edited but not brought back: change 'end'" in text


def test_trimming_keeps_unchanged_keyframes_on_the_source(exported):
    _editor, out, original, clips, _files = exported
    t = _load(out)
    _entry(t, clips["video"])["start"] = 2.25                          # trim into the fade-in (snapped)
    _save(out, t)
    project, warnings, _ = _restore(out)
    v = _clip(project, clips["video"])
    assert v["start"] == pytest.approx(68 / 30) and v["alpha"] == _clip(original, clips["video"])["alpha"] and warnings == []
