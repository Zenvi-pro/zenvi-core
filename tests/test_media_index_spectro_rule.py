"""When a picture of the sound is worth asking for: strip planning, uncertain section edges, the suggestion, and the timeline mix view."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import audio as au, musicfit, review as R, spectro  # noqa: E402
from eval import corpus  # noqa: E402


# ============================ strips ============================
def test_a_range_is_cut_into_strips_of_at_most_a_minute_covering_all_of_it():
    assert spectro.plan_strips(0, 45) == [[0.0, 45.0]]
    assert spectro.plan_strips(10, 130) == [[10.0, 70.0], [70.0, 130.0]]
    strips = spectro.plan_strips(0, 150)
    assert len(strips) == 3 and strips[0][0] == 0.0 and strips[-1][1] == 150.0 and all(b - a <= spectro.MAX_SECONDS for a, b in strips)
    assert all(strips[i][1] == strips[i + 1][0] for i in range(len(strips) - 1)), "no gap and no overlap"


def test_a_range_needing_more_than_four_strips_says_what_to_ask_for_instead():
    assert len(spectro.plan_strips(0, 240)) == 4
    with pytest.raises(ValueError, match=r"needs 5 strips.*at most 4.*start=0, end=240"):
        spectro.plan_strips(0, 241)


def test_strips_are_held_to_the_file_and_a_sliver_is_refused():
    assert spectro.plan_strips(0, 500, duration=50) == [[0.0, 50.0]]
    with pytest.raises(ValueError, match="too short"):
        spectro.plan_strips(49.8, 50.0, duration=50)


# ============================ edge confidence ============================
def test_a_ramp_edge_is_uncertain_and_a_sharp_change_is_sure():
    assert au.edge_confidence("ramp", None) == au.RAMP_EDGE_CONFIDENCE < spectro.LOW_EDGE_CONFIDENCE
    assert au.edge_confidence("novelty", 2.0) == 1.0 and au.edge_confidence("novelty", 1.0) == 0.0 and au.edge_confidence("novelty", 1.5) == 0.5
    assert au.edge_confidence("novelty", 9.0) == 1.0, "capped"


def test_merged_edges_keep_where_each_came_from():
    edges = au.merge_bounds_detailed([(20.0, 2.0), (40.0, 1.2)], [30.0])
    assert [(e["t"], e["source"]) for e in edges] == [(20.0, "novelty"), (30.0, "ramp"), (40.0, "novelty")]
    assert [e["confidence"] for e in edges] == [1.0, au.RAMP_EDGE_CONFIDENCE, 0.2]


def test_real_music_sections_carry_a_source_and_confidence_on_each_edge():
    path, truth = corpus.song(bpm=100.0, seconds=60.0, seed=3, sections=[
        dict(start=0, end=20, level=0.3, bed=220, click=900, hat=False), dict(start=20, end=40, level=(0.2, 0.9), bed=330, click=1500, hat=True),
        dict(start=40, end=60, level=0.5, bed=440, click=700, hat=False)])
    from classes.media_index.probe import probe_media
    secs = au.analyze_audio(str(path), probe_media(str(path)))["music"]["sections"]
    assert len(secs) >= 2 and "confidence" not in secs[0]
    for s in secs[1:]:
        assert s["source"] in ("ramp", "novelty") and 0.0 <= s["confidence"] <= 1.0


# ============================ the suggestion ============================
def sec(start, conf, source="novelty"):
    return {"label": "x", "start": start, "confidence": conf, "source": source}


def test_nothing_is_suggested_when_every_edge_is_sure():
    assert spectro.suggest([{"label": "intro", "start": 0}, sec(20, 1.0), sec(40, 0.8)], 60) is None
    assert spectro.suggest([{"label": "intro", "start": 0}], 60) is None and spectro.suggest([], 60) is None


def test_an_uncertain_edge_gets_a_range_around_it_and_a_reason():
    s = spectro.suggest([{"label": "intro", "start": 0}, sec(20, 1.0), sec(38, au.RAMP_EDGE_CONFIDENCE, "ramp")], 60)
    assert s["ranges"] == [[33.0, 43.0]] and s["edges"] == [{"at": 38, "confidence": 0.4, "source": "ramp"}] and "view_audio_tool" in s["why"]


def test_nearby_uncertain_edges_share_one_range_and_it_stops_at_the_ends_of_the_file():
    s = spectro.suggest([{"label": "a", "start": 0}, sec(3, 0.4, "ramp"), sec(9, 0.4, "ramp"), sec(58, 0.4, "ramp")], 60)
    assert s["ranges"] == [[0.0, 14.0], [53.0, 60.0]]


def test_the_boundary_confidence_is_exclusive():
    assert spectro.suggest([{"label": "a", "start": 0}, sec(20, spectro.LOW_EDGE_CONFIDENCE)], 60) is None
    assert spectro.suggest([{"label": "a", "start": 0}, sec(20, spectro.LOW_EDGE_CONFIDENCE - 0.01)], 60) is not None


def test_the_music_profile_carries_the_suggestion():
    audio = {"tempo": {"bpm": 100}, "music": {"sections": [{"label": "intro", "start": 0}, sec(20, 0.4, "ramp")]}}
    assert musicfit.profile_from_audio(audio, 60)["spectrogramSuggested"]["ranges"] == [[15.0, 25.0]]
    audio["music"]["sections"][1]["confidence"] = 1.0
    assert musicfit.profile_from_audio(audio, 60)["spectrogramSuggested"] is None


# ============================ the timeline mix view ============================
@pytest.fixture
def timeline(monkeypatch, tmp_path):
    from classes.editor_tools import media_index_tools as T
    from classes.editor_tools import media_index_tools_review as TR
    clips = [
        R.TimelineClip(id="V", name="talk", file_id="f1", sha="a" * 64, layer=1, kind="video", start=0.0, end=6.0, src_in=0.0, src_out=6.0, role="speech", gain_db=0.0, has_audio=True),
        R.TimelineClip(id="M", name="song", file_id="f2", sha="b" * 64, layer=2, kind="audio", start=0.0, end=6.0, src_in=10.0, src_out=16.0, role="music", gain_db=-12.0, has_audio=True),
        R.TimelineClip(id="T", name="title", layer=3, kind="title", start=1.0, end=3.0, has_audio=False),
    ]
    index = {"a" * 64: SimpleNamespace(audio={"windows": [{"start": float(i), "end": i + 1.0, "rms_db": -20.0} for i in range(10)]}),
             "b" * 64: SimpleNamespace(audio={"windows": [{"start": float(i), "end": i + 1.0, "rms_db": -14.0} for i in range(30)]})}
    monkeypatch.setattr(TR, "build_timeline", lambda: (clips, R.ProjectInfo(duration=6.0), {}))
    monkeypatch.setattr(T.library, "get_file_index", lambda shelf, sha, **kw: index.get(sha))
    rendered = []

    def fake_render(lo, hi):
        import subprocess
        path = str(tmp_path / f"mix_{len(rendered)}.wav")
        subprocess.run([corpus.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=500:duration={hi - lo}", path], check=True)
        rendered.append((lo, hi, path))
        return path, ""

    monkeypatch.setattr(TR, "render_timeline_mix", fake_render)
    return SimpleNamespace(clips=clips, rendered=rendered)


def view(**kw):
    out = REGISTRY["view_audio_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


def test_the_timeline_view_draws_the_rendered_mix_and_gives_each_clips_level_as_numbers(timeline):
    head, r = view(timeline=True, start=0.0, end=4.0)
    assert Path(r["image_path"]).is_file() and r["cached"] is False and "numbers" in r["numbers_first"]
    by = {x["clip"]: x for x in r["levels"]}
    assert set(by) == {"V", "M"}, "the title has no sound and is left out"
    assert [p["db"] for p in by["V"]["per_second"]] == [-20.0] * 4, "the voice: source level plus 0 dB"
    assert [p["db"] for p in by["M"]["per_second"]] == [-26.0] * 4, "the music: -14 dB source with -12 dB set"
    assert timeline.rendered[0][:2] == (0.0, 4.0) and not Path(timeline.rendered[0][2]).exists(), "the render is not kept"


def test_the_timeline_view_needs_a_range_and_holds_the_strip_limits(timeline):
    assert "give start and end" in REGISTRY["view_audio_tool"].func(timeline=True)
    assert "needs 5 strips" in REGISTRY["view_audio_tool"].func(timeline=True, start=0.0, end=300.0)
    assert "past the end" in REGISTRY["view_audio_tool"].func(timeline=True, start=50.0, end=60.0)


def test_a_timeline_range_longer_than_the_timeline_is_clamped_to_it(timeline):
    _, r = view(timeline=True, start=2.0, end=100.0)
    assert r["end"] == 6.0 and timeline.rendered[0][:2] == (2.0, 6.0)


def test_a_ducked_clip_is_flagged_and_its_level_follows_the_curve(timeline):
    music = next(c for c in timeline.clips if c.id == "M")
    music.gain_db, music.gain_fn = None, (lambda t: -6.0 if t < 2.0 else -18.0)
    _, r = view(timeline=True, start=0.0, end=4.0)
    m = next(x for x in r["levels"] if x["clip"] == "M")
    assert m["volume_automated"] is True and [p["db"] for p in m["per_second"]] == [-20.0, -20.0, -32.0, -32.0]
    assert next(x for x in r["levels"] if x["clip"] == "V")["volume_automated"] is False
