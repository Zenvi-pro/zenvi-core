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
    # a new lane changes nothing HyperFrames paints, so the clip keeps its track (and says so)
    assert (ci["position"], ci["start"], ci["end"], ci["layer"]) == (1.5, 0.0, 1.0, 2000000)
    assert any("stays on its Zenvi track" in w for w in plan.warnings)
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


def test_split_and_duplicated_elements_come_back_as_copies(tmp_path):
    """Review C5-1 #4: Studio's split / duplicate clone the element with its data-zenvi-clip-id."""
    res, _snap, _html = export(tmp_path, project(tmp_path, _clips))
    index = os.path.join(res.output_dir, "index.html")
    with open(index, encoding="utf-8") as fh:
        html = fh.read()
    video = re.search(r'<video id="c-CV"[^>]*></video>', html).group(0)
    image = re.search(r'<img id="c-CI"[^>]*/>', html).group(0)
    # split the video at 1.5 s: the original ends there, the clone starts there 1.5 s further into the media
    first = video.replace('data-duration="3"', 'data-duration="1.5"')
    second = (video.replace('id="c-CV"', 'id="c-CV-b"').replace('data-start="0"', 'data-start="1.5"')
              .replace('data-duration="3"', 'data-duration="1.5"')
              .replace('data-media-start="0.5"', 'data-media-start="0.5" data-playback-start="2"'))
    # the clone is listed before the original: the exported id says which one is the original
    copy = image.replace('id="c-CI"', 'id="c-CI-copy"').replace('data-start="1"', 'data-start="5"')
    html = html.replace(video, second + first).replace(image, image + copy)
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(html)
    plan = hfrestore.plan_restore(hfp.load_project(res.output_dir))
    clips = plan.project["clips"]
    assert len(clips) == 5 and sorted(plan.copied) == ["CI", "CV"]
    by_id = {c["id"]: c for c in clips if c["id"]}
    copies = [c for c in clips if not c["id"]]
    assert set(by_id) == {"CV", "CI", "CA"} and len(copies) == 2
    cv, ci = by_id["CV"], by_id["CI"]
    assert (cv["position"], cv["start"], cv["end"]) == (0.0, 0.5, 2.0)
    v2 = next(c for c in copies if c["file_id"] == "FV")
    assert (v2["position"], v2["start"], v2["end"]) == (1.5, 2.0, 3.5)          # in point from playback-start
    i2 = next(c for c in copies if c["file_id"] == "FI")
    assert (ci["position"], i2["position"]) == (1.0, 5.0)
    assert i2["alpha"] == ci["alpha"] and i2["location_x"] == ci["location_x"]   # the clip's look, copied
    assert any("split or duplicated" in w for w in plan.warnings)
