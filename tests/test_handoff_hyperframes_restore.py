"""A Zenvi export coming back from HyperFrames: lossless restore, edits made in HyperFrames, frame rates."""

import os
import re
import shutil
from fractions import Fraction

import pytest

from classes.handoff.hyperframes import parser as hfp
from classes.handoff.hyperframes import restore as hfrestore
from test_handoff_hyperframes_exporter import export, kf, project


def _clips(clip, v, i, a, t):
    return [clip("CV", v, 1000000, 0.0, 0.5, 3.5, volume=kf((16, 1.0), (106, 0.0, 1))),
            clip("CI", i, 2000000, 1.0, 0.0, 2.0, location_x=kf((1, -0.4), (19, -0.3, 0, (0.16, 1.0), (0.3, 1.0))),
                 alpha=kf((1, 0.0), (16, 1.0, 1))),
            clip("CA", a, 1000000, 3.5, 0.0, 1.5, has_video=kf((1, 0.0, 2)))]


def _edit(index, clip_id, **attrs):
    with open(index, encoding="utf-8") as fh:
        html = fh.read()
    m = re.search(r'<(video|img|audio) id="c-%s"[^>]*>' % clip_id, html)
    tag = m.group(0)
    new = tag
    for name, value in attrs.items():
        name = name.replace("_", "-")
        if value is None:
            new = re.sub(r' %s="[^"]*"' % name, "", new)
        elif re.search(r' %s="' % name, new):
            new = re.sub(r' %s="[^"]*"' % name, ' %s="%s"' % (name, value), new)
        else:
            new = new.replace(' data-zenvi-clip-id', ' %s="%s" data-zenvi-clip-id' % (name, value))
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(html.replace(tag, new))


def test_round_trip_is_lossless(tmp_path):
    proj = project(tmp_path, _clips)
    res, _snap, _html = export(tmp_path, proj)
    plan = hfrestore.plan_restore(hfp.load_project(res.output_dir), target_fps=Fraction(30))
    original = {c["id"]: c for c in proj["clips"]}
    restored = {c["id"]: c for c in plan.project["clips"]}
    assert set(restored) == set(original)
    for cid, c in original.items():
        assert restored[cid] == c, cid  # every key, keyframe and handle
    assert [f["path"] for f in plan.project["files"]] == [f["path"] for f in proj["files"]]  # originals exist
    assert plan.project["markers"] == proj["markers"] and plan.project["layers"] == proj["layers"]
    assert not plan.edited and not plan.dropped and not plan.warnings


def test_moved_project_uses_the_copies(tmp_path):
    proj = project(tmp_path, _clips)
    res, _snap, _html = export(tmp_path, proj)
    shutil.rmtree(str(tmp_path / "media"))  # the originals are gone (another machine)
    plan = hfrestore.plan_restore(hfp.load_project(res.output_dir))
    paths = {f["id"]: f["path"] for f in plan.project["files"]}
    assert paths["FV"] == os.path.join(res.output_dir, "assets", "clip.mp4")
    clip = next(c for c in plan.project["clips"] if c["id"] == "CV")
    assert clip["reader"]["path"] == paths["FV"]


def test_edits_made_in_hyperframes_come_back(tmp_path):
    res, _snap, _html = export(tmp_path, project(tmp_path, _clips))
    index = os.path.join(res.output_dir, "index.html")
    _edit(index, "CI", data_start="1.5", data_duration="1", data_track_index="0")
    _edit(index, "CV", data_media_start="1")
    with open(index, encoding="utf-8") as fh:
        html = fh.read()
    html = re.sub(r'\s*<audio id="c-CA"[^>]*></audio>', "", html)  # deleted in Studio
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(html)
    plan = hfrestore.plan_restore(hfp.load_project(res.output_dir))
    clips = {c["id"]: c for c in plan.project["clips"]}
    assert set(clips) == {"CV", "CI"} and plan.dropped == ["CA"] and sorted(plan.edited) == ["CI", "CV"]
    ci, cv = clips["CI"], clips["CV"]
    assert (ci["position"], ci["start"], ci["end"], ci["layer"]) == (1.5, 0.0, 1.0, 1000000)
    assert (cv["position"], cv["start"], cv["end"]) == (0.0, 1.0, 4.0)
    assert ci["location_x"]["Points"][1]["handle_left"] == {"X": 0.3, "Y": 1.0}  # keyframes untouched
    assert any("deleted in HyperFrames" in w for w in plan.warnings)


def test_frame_rate_change_rescales_keyframes(tmp_path):
    res, _snap, _html = export(tmp_path, project(tmp_path, _clips))
    plan = hfrestore.plan_restore(hfp.load_project(res.output_dir), target_fps=Fraction(60))
    ci = next(c for c in plan.project["clips"] if c["id"] == "CI")
    assert [p["co"]["X"] for p in ci["alpha"]["Points"]] == [1.0, 31.0]
    assert any("rescaled" in w for w in plan.warnings)


def test_damaged_or_foreign_timeline_blocks(tmp_path):
    res, _snap, _html = export(tmp_path, project(tmp_path, _clips))
    p = hfp.load_project(res.output_dir)
    p.zenvi_timeline = {"zenvi_timeline": 9, "zenvi": {"project": {}}}
    with pytest.raises(hfrestore.RestoreError, match="newer Zenvi"):
        hfrestore.plan_restore(p)
    p.zenvi_timeline = {"zenvi_timeline": 1}
    with pytest.raises(hfrestore.RestoreError, match="holds no Zenvi project"):
        hfrestore.plan_restore(p)
    p.zenvi_timeline = None
    with pytest.raises(hfrestore.RestoreError, match="not a Zenvi export"):
        hfrestore.plan_restore(p)
