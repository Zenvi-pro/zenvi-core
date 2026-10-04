"""The audit's colour measurement through real libopenshot rendering: real clips, real grades, real numbers.

Needs the real libopenshot module (ZENVI_REAL_QT=1 with openshot on PYTHONPATH); skipped under the headless stub.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

openshot = pytest.importorskip("openshot")
if not hasattr(openshot, "EffectInfo"):
    pytest.skip("needs the real libopenshot module", allow_module_level=True)
pytest.importorskip("PyQt5.QtGui")

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

import media_fixtures as mf  # noqa: E402
from classes.editor_tools import media_index_tools_review as TR  # noqa: E402
from classes.media_index import review as R  # noqa: E402

PICTURE = 1000000


def solid_video(path, color, seconds=2, size="320x180"):
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={seconds}:r=15",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)], check=True)
    return str(path)


@pytest.fixture
def world(editor, tmp_path, monkeypatch):
    from classes import clip_utils
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.add_track(PICTURE, "Picture")
    editor.app.project.get  # the harness project answers width, height, fps
    dark = solid_video(tmp_path / "dark.mp4", "0x181818")
    bright = solid_video(tmp_path / "bright.mp4", "0xd8d8d8")
    ids = {}
    for name, path in (("dark", dark), ("bright", bright)):
        f = editor.add_file("video", path=path, duration=2.0, width=320, height=180)
        ids[name] = editor.add_clip(f, position=0.0 if name == "dark" else 2.0, layer=PICTURE, start=0.0, end=2.0)
    editor.mark()
    return editor, ids


def measure(editor, ids):
    clips, _project, objs = TR.build_timeline()
    looks, considered = TR.measure_looks(clips, objs, max_clips=10)
    return clips, objs, looks


def test_real_rendering_measures_a_dark_and_a_bright_clip_and_the_audit_calls_them_inconsistent(world):
    editor, ids = world
    clips, _objs, looks = measure(editor, ids)
    assert set(looks) == {ids["dark"], ids["bright"]}, "both clips rendered and measured"
    dark, bright = looks[ids["dark"]], looks[ids["bright"]]
    assert dark["present"] and bright["present"] and dark["avg_luma"] < 0.2 < 0.7 < bright["avg_luma"]
    result = R.review(clips, R.ProjectInfo(duration=4.0), R.Brief(), index_for=lambda c: None, looks=looks)
    consistency = {f["id"]: f for layer in result["layers"].values() for f in layer}["consistency"]
    assert consistency["status"] == "needs" and consistency["evidence"]["luma_spread"] > 0.4
    assert {o["clip"] for o in consistency["evidence"]["outliers"]} == {ids["dark"], ids["bright"]}


def test_a_real_grade_changes_what_the_audit_measures(world):
    editor, ids = world
    before = measure(editor, ids)[2][ids["dark"]]["avg_luma"]
    receipt = editor.call_receipt("apply_color_tool", clipId=ids["dark"], exposure_delta=0.35)
    assert receipt["status"] == "applied", receipt["summary"]
    after = measure(editor, ids)[2][ids["dark"]]["avg_luma"]
    assert after > before * 1.2 and after > before + 0.02, f"the grade is measured after it is applied: {before:.3f} -> {after:.3f}"
    assert any(e == "ColorGrade" for c in measure(editor, ids)[0] if c.id == ids["dark"] for e in c.effects), "the graded clip reports its ColorGrade"


def test_the_harmonize_tool_pulls_the_strays_toward_the_median_look_for_real(world, monkeypatch):
    editor, ids = world
    dry = editor.call_receipt("harmonize_look_tool", dry_run=True)
    assert dry["status"] in ("applied", "unchanged") and dry["data"]["dry_run"] is True and len(dry["data"]["distances"]) == 1
    only = next(iter(dry["data"]["distances"].values()))
    assert only > 0.15, f"a dark and a bright clip are far apart ({only:.2f})"
    assert dry["data"]["to_match"], "the stray is chosen"
    assert editor.undo_steps_since_mark() == 0


def test_harmonize_really_matches_the_stray_clip_in_one_undo_step_and_undo_restores_it(world):
    editor, ids = world
    before_effects = {cid: list((editor.clip(cid) or {}).get("effects") or []) for cid in ids.values()}
    editor.mark()
    receipt = editor.call_receipt("harmonize_look_tool", reference=ids["dark"], tolerance=0.05)
    assert receipt["status"] == "applied", receipt["summary"]
    data = receipt["data"]
    assert data["reference"] == ids["dark"] and data["to_match"] == [ids["bright"]] and data["changed"] is True
    assert data["after"][ids["bright"]] is not None and data["after"][ids["bright"]] < data["distances"][ids["bright"]], \
        f"the bright clip is closer to the dark reference afterwards: {data['distances'][ids['bright']]} -> {data['after'][ids['bright']]}"
    assert ids["bright"] in data["improved"] and ids["bright"] in data["still_far"], "one pass moves a very different clip closer but not all the way: the tool says so"
    assert editor.undo_steps_since_mark() >= 1
    assert any(e.get("class_name") == "ColorGrade" for e in editor.clip(ids["bright"])["effects"]), "a real grade was added to the stray"
    editor.undo()
    assert {cid: list((editor.clip(cid) or {}).get("effects") or []) for cid in ids.values()} == before_effects
