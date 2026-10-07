"""review_edit, get_project_overview, analyze_music and listen: the tool layer around the audit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

import media_fixtures as mf  # noqa: E402
from classes.editor_tools import REGISTRY, media_index_tools as T, media_index_tools_review as TR  # noqa: E402
from classes.editor_tools._base import ToolError  # noqa: E402
from classes.media_index import audio_cloud, library, review as R  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from test_media_index_review import FakeIndex, clip, look  # noqa: E402
from test_media_index_search import SHA1, SHA2, build, profile, shot, w  # noqa: E402


def call(name, **kw):
    out = REGISTRY[name].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


# ============================ review_edit_tool ============================
@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    monkeypatch.setattr(T, "default_shelf", lambda: shelf)
    monkeypatch.setattr(TR, "default_shelf", lambda: shelf)
    state = SimpleNamespace(clips=[], objs={}, looks=({}, 0), mix_path=None, mix_why="", loudness=None, renders=[], shelf=shelf)
    project = R.ProjectInfo(duration=20.0, width=1080, height=1920)
    monkeypatch.setattr(TR, "build_timeline", lambda: (state.clips, project, state.objs))
    monkeypatch.setattr(TR, "measure_looks", lambda clips, objs, **kw: state.looks)
    monkeypatch.setattr(TR, "caption_intervals", lambda objs: [])

    def fake_render(start, end):
        state.renders.append((start, end))
        return state.mix_path, state.mix_why

    monkeypatch.setattr(TR, "render_timeline_mix", fake_render)
    from classes.media_index import audio as au
    monkeypatch.setattr(au, "measure_loudness", lambda path: state.loudness)
    return state


def test_review_reports_needs_unknowns_and_what_was_measured(stubbed):
    stubbed.clips = [clip("a", 0, 10, name="a"), clip("b", 10, 20, name="b"), clip("talk", 0, 10, kind="audio", role="speech")]
    stubbed.looks = ({"a": look(0.5), "b": look(0.85)}, 2)
    stubbed.mix_path = None
    stubbed.mix_why = "an export is already running"
    head, r = call("review_edit_tool", form="TikTok", vibe="warm")
    assert "need work" in head and "consistency" in r["needs"] and "style" in r["needs"] and "captions" in r["needs"]
    assert "loudness" in r["unknown"]
    assert r["measured"] == {"colour_clips": 2, "colour_clips_considered": 2, "mix_rendered": False, "mix_note": "an export is already running",
                             "duration": 20.0, "clips": 3}
    assert r["counts"]["needs"] == len(r["needs"]) and set(r["layers"]) >= {"story", "picture", "sound"}


def test_a_rendered_mix_is_measured_and_its_temporary_file_removed(stubbed, tmp_path):
    folder = tmp_path / "mixdir"
    folder.mkdir()
    mix = folder / "mix.mp3"
    mix.write_bytes(b"x" * 100)
    stubbed.clips = [clip("a", 0, 20), clip("bed", 0, 20, kind="audio", role="music", layer=2)]
    stubbed.mix_path = str(mix)
    stubbed.loudness = {"integrated_lufs": -20.5, "lra": 4.0, "true_peak_db": -3.0}
    _, r = call("review_edit_tool", form="vlog")
    f = {x["id"]: x for layer in r["layers"].values() for x in layer}["loudness"]
    assert f["status"] == "needs" and f["evidence"]["integrated_lufs"] == -20.5 and r["measured"]["mix_rendered"] is True
    assert not mix.exists() and not folder.exists(), "the analysis render leaves nothing behind"
    assert stubbed.renders == [(0.0, 20.0)]


def test_the_mix_is_only_rendered_when_asked_for_and_when_there_is_audio(stubbed):
    stubbed.clips = [clip("a", 0, 20)]                                    # a silent picture
    call("review_edit_tool")
    assert stubbed.renders == []
    stubbed.clips = [clip("a", 0, 20), clip("bed", 0, 20, kind="audio", role="music", layer=2)]
    call("review_edit_tool", render_mix=False)
    assert stubbed.renders == []


def test_colour_is_only_measured_when_asked_for(stubbed, monkeypatch):
    stubbed.clips = [clip("a", 0, 10), clip("b", 10, 20)]
    seen = []
    monkeypatch.setattr(TR, "measure_looks", lambda *a, **k: seen.append(k) or ({}, 0))
    call("review_edit_tool", measure_colour=False)
    assert seen == []
    call("review_edit_tool", max_clips_measured=7)
    assert seen[0]["max_clips"] == 7


def test_an_empty_timeline_is_an_error(stubbed):
    out = REGISTRY["review_edit_tool"].func()
    assert out.startswith("Error") and "empty" in out


def test_a_clean_summary_says_so_and_names_what_could_not_be_measured(stubbed):
    stubbed.clips = [clip("a", 0, 20), clip("room", 0, 20, kind="audio", role="ambient", layer=2)]
    head, r = call("review_edit_tool", measure_colour=False, render_mix=False)
    assert r["needs"] == [] and head.startswith("Nothing needs work") and "could not be measured" in head and "hook" in head
    assert "how_to_read" in r


def test_the_brief_flags_become_the_audits_expectations(stubbed):
    stubbed.clips = [clip("a", 0, 40)]
    _, r = call("review_edit_tool", form="vlog", target_seconds=60, wants_music="no", wants_grade="yes", measure_colour=False, render_mix=False)
    ids = {x["id"]: x for layer in r["layers"].values() for x in layer}
    assert ids["length"]["status"] == "needs" and ids["music"]["status"] == "ok" and ids["style"]["status"] == "needs"


# ============================ measuring colour ============================
def objs_for(clips):
    return {c.id: SimpleNamespace(data={"id": c.id}) for c in clips}


def test_every_visible_clip_is_measured_once(monkeypatch):
    clips = [clip("base", 0, 12), clip("pip", 4, 6, layer=2), clip("c", 12, 20)]
    called = []
    looks, considered = TR.measure_looks(clips, objs_for(clips), max_clips=24, profile_fn=lambda d: called.append(d["id"]) or {"profile": {"present": True, "avg_luma": 0.5}})
    assert sorted(called) == ["base", "c", "pip"] and considered == 3 and set(looks) == {"base", "c", "pip"}


def test_too_many_clips_are_sampled_evenly_with_the_first_and_last_kept():
    clips = [clip(f"c{i}", i * 2.0, i * 2.0 + 2.0) for i in range(30)]
    called = []
    looks, considered = TR.measure_looks(clips, objs_for(clips), max_clips=6, profile_fn=lambda d: called.append(d["id"]) or {"profile": {"present": True}})
    assert considered == 6 and called[0] == "c0" and called[-1] == "c29" and len(set(called)) == 6
    assert called == sorted(called, key=lambda x: int(x[1:]))


def test_the_time_budget_stops_measuring_and_a_failing_clip_does_not_sink_the_rest():
    clips = [clip(f"c{i}", i * 2.0, i * 2.0 + 2.0) for i in range(5)]

    def flaky(d):
        if d["id"] == "c1":
            raise RuntimeError("cannot render")
        return {"profile": {"present": True}}

    looks, _ = TR.measure_looks(clips, objs_for(clips), max_clips=24, profile_fn=flaky)
    assert set(looks) == {"c0", "c2", "c3", "c4"}
    none, considered = TR.measure_looks(clips, objs_for(clips), max_clips=24, budget=-1.0, profile_fn=lambda d: {"profile": {"present": True}})
    assert none == {} and considered == 5


def test_an_unpresent_profile_is_not_a_measurement():
    clips = [clip("a", 0, 5)]
    looks, _ = TR.measure_looks(clips, objs_for(clips), max_clips=4, profile_fn=lambda d: {"profile": {"present": False}})
    assert looks == {}


# ============================ captions and clip kinds ============================
def test_caption_cues_become_timeline_intervals():
    data = {"position": 10.0, "start": 0.0, "end": 8.0,
            "effects": [{"class_name": "Caption", "caption_text": "00:00:00:500 --> 00:00:03:000\nHello there\n\n00:00:04:000 --> 00:00:06:000\nSecond line"}]}
    got = TR.caption_intervals({"c": SimpleNamespace(data=data)})
    assert got == [pytest.approx((10.5, 13.0)), pytest.approx((14.0, 16.0))]
    trimmed = dict(data, start=2.0, position=0.0)
    assert TR.caption_intervals({"c": SimpleNamespace(data=trimmed)})[0] == pytest.approx((-1.5, 1.0)), "cues are in source time; trimming shifts them"
    assert TR.caption_intervals({"c": SimpleNamespace(data={"effects": []})}) == []


@pytest.mark.parametrize("media,path,kind", [("video", "a.mp4", "video"), ("audio", "a.mp3", "audio"), ("image", "a.png", "image"),
                                             ("image", "title.svg", "title"), ("title", "t.png", "title"), (None, "a.mov", "video"), ("weird", "a.x", "video")])
def test_clip_kinds_come_from_the_file_type_and_the_reader_path(media, path, kind):
    assert TR._file_kind({"media_type": media} if media else None, {"reader": {"path": path}}) == kind


# ============================ the survey ============================
def load(shelf, sha, fid, name):
    return library.load_file_index(shelf, sha, file_id=fid, name=name)


LAST = {}


@pytest.fixture
def footage(tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "s"))
    LAST["shelf"] = shelf
    build(shelf, SHA1, shots=[shot(0, 0, 10, "pan", _look=profile(0.2, -0.1)), shot(1, 10, 20, "static", _look=profile(0.7, 0.2))], duration=20.0,
          watch=[w(0, 0, 10, "A calm lake", interest=0.4, highlight_reason="pleasant"), w(1, 10, 20, "A dog runs", interest=0.9, highlight_reason="funny splash")],
          look={"profile": profile(0.2, -0.1)},
          text=[], image=[])
    shelf.set_source(SHA1, duration=20.0, media_type="video", orientation="landscape", captured_at="2024-05-01T09:00:00+00:00", gps={"lat": 37.7749, "lon": -122.4194})
    build(shelf, SHA2, shots=[shot(0, 0, 8, "static", black=False), shot(1, 8, 12, black=True)], duration=12.0, orientation="portrait",
          watch=[w(0, 0, 8, "A kitchen", usable=False), w(1, 8, 12, "")], look={"profile": profile(0.7, 0.2)})
    shelf.set_source(SHA2, duration=12.0, media_type="video", orientation="portrait", captured_at="2024-05-03T10:00:00+00:00", gps={"lat": 35.6762, "lon": 139.6503})
    song = "c" * 64
    shelf.set_source(song, duration=40.0, media_type="audio")
    shelf.write_json(song, "audio.json", {"tempo": {"bpm": 110.0, "beats": [1.0, 1.5]}, "loudness": {"integrated_lufs": -14.0},
                                           "music": {"arc": [0.1, 0.9, 0.2], "sections": [{"label": "intro", "start": 0.0, "end": 10.0}, {"label": "peak", "start": 10.0, "end": 30.0}]}})
    shelf.set_layer(song, "audio", version=2, status="ready")
    return [load(shelf, SHA1, "F1", "lake.mp4"), load(shelf, SHA2, "F2", "kitchen.mp4"), load(shelf, song, "M1", "song.mp3")]


def test_the_survey_counts_what_there_is(footage):
    s = TR.survey(footage, ["new.mp4"])
    t = s["totals"]
    assert (t["files"], t["videos"], t["audio"], t["images"], t["not_indexed"]) == (3, 2, 1, 0, 1) and t["video_minutes"] == 0.5
    assert s["not_indexed"] == ["new.mp4"] and s["orientation"] == {"landscape": 1, "portrait": 1}


def test_the_best_moments_are_ranked_and_skip_black_and_unusable_shots(footage):
    s = TR.survey(footage, [], top=5)
    names = [(m["file_id"], m["start"]) for m in s["top_moments"]]
    assert names[0] == ("F1", 10.0), "the funny splash outranks the calm lake"
    assert ("F2", 8.0) not in names and ("F2", 0.0) not in names, "a black shot and a shot the model called unusable are not offered"
    assert s["top_moments"][0]["why"] == "funny splash"
    assert len(TR.survey(footage, [], top=1)["top_moments"]) == 1


def test_days_and_places_come_from_capture_tags_with_city_level_coordinates(footage):
    s = TR.survey(footage, [])
    assert [(d["day"], d["date"]) for d in s["trip"]["days"]] == [(1, "2024-05-01"), (2, "2024-05-03")]
    assert [p["name"] for p in s["trip"]["places"]] == ["San Francisco, CA, United States", "Tokyo, Japan"], "named places are given by name"
    assert all("lat" not in p and "lon" not in p for p in s["trip"]["places"]), "and without coordinates"
    assert "city level" in s["trip"]["place_precision"]
    exact = TR.survey(footage, [], precise_places=True)
    assert exact["trip"]["places"][0]["lat"] == 37.7749 and exact["trip"]["places"][0]["name"] and exact["trip"]["place_precision"] == "exact"


def test_music_files_and_look_clusters_are_summarised(footage):
    s = TR.survey(footage, [])
    assert s["music"][0]["bpm"] == 110.0 and s["music"][0]["sections"][1] == ("peak", 10.0) and s["music"][0]["described"] is False
    assert s["looks"] == {"dark / cool": {"files": 1, "ids": ["F1"]}, "bright / warm": {"files": 1, "ids": ["F2"]}}


def test_the_same_footage_twice_is_surveyed_once(footage):
    twin = library.load_file_index(LAST["shelf"], SHA1, file_id="F1-COPY", name="lake copy.mp4")
    s = TR.survey([footage[0], twin, footage[1]], [])
    assert s["totals"]["files"] == 2 and s["totals"]["videos"] == 2
    assert [m["file_id"] for m in s["top_moments"] if m["start"] == 10.0] == ["F1"], "the first file stands for the footage"


def test_the_overview_tool_reports_and_refuses_an_empty_project(footage, monkeypatch):
    monkeypatch.setattr(T, "project_indexes", lambda file_ids=None: (footage, ["new.mp4"]))
    monkeypatch.setattr(TR, "project_indexes", lambda file_ids=None: (footage, ["new.mp4"]))
    head, r = call("get_project_overview_tool", top_moments=2)
    assert "3 indexed file(s), 1 not indexed yet" in head and len(r["top_moments"]) == 2 and r["totals"]["files"] == 3
    monkeypatch.setattr(TR, "project_indexes", lambda file_ids=None: ([], []))
    assert REGISTRY["get_project_overview_tool"].func().startswith("Error")


# ============================ analyze_music_tool ============================
def music_env(monkeypatch, footage, tmp_path):
    file = SimpleNamespace(id="M1", data={"name": "song.mp3", "path": str(tmp_path / "song.mp3"), "media_type": "audio"})
    monkeypatch.setattr(TR, "resolve_files", lambda ids, query="": [file])
    monkeypatch.setattr(TR, "_index_for", lambda f: footage[2])
    return file


def test_a_music_profile_carries_tempo_energy_sections_and_phrase_points(footage):
    p = TR.music_profile_of(footage[2])
    assert p["bpm"] == 110.0 and p["energy_arc"] == [0.1, 0.9, 0.2] and [s["label"] for s in p["sections"]] == ["intro", "peak"] and p["beats"] == 2


@pytest.mark.parametrize("kwargs,fits,fragment", [
    (dict(bpm_min=100, bpm_max=120), True, "inside the wanted range"), (dict(bpm_min=120, bpm_max=140), False, "outside"),
    (dict(seconds=30), True, "long enough"), (dict(seconds=90), False, "shorter than"),
    (dict(energy="building"), False, "does not build"), (dict(energy="low"), True, "within the low range"), (dict(energy="high"), False, "outside the high range")])
def test_music_fit_gives_a_measured_reason_for_each_wish(kwargs, fits, fragment):
    profile = {"bpm": 110.0, "seconds": 40.0, "energy_arc": [0.5, 0.3, 0.2, 0.1], "phrase_points": []}
    args = dict(bpm_min=None, bpm_max=None, seconds=None, energy="")
    args.update(kwargs)
    got = TR.music_fit(profile, **args)
    assert got["fits"] is fits and any(fragment in n for n in got["notes"])


def test_a_track_with_no_tempo_cannot_fit_a_tempo_wish_and_only_a_rising_track_builds():
    assert TR.music_fit({"bpm": None}, bpm_min=90, bpm_max=None, seconds=None, energy="")["fits"] is False
    build_ = lambda arc: TR.music_fit({"energy_arc": arc}, bpm_min=None, bpm_max=None, seconds=None, energy="building")["fits"]  # noqa: E731
    assert build_([0.1, 0.2, 0.8, 0.9]) is True and build_([0.1, 0.3, 0.5, 0.7, 0.9, 1.0]) is True
    assert build_([0.1, 0.9, 0.2]) is False, "a peak in the middle that falls away is not a build"
    assert build_([0.9, 0.6, 0.3, 0.1]) is False and build_([0.5, 0.5, 0.5, 0.5]) is False and build_([0.1, 0.9]) is False


def test_a_short_track_with_phrase_points_can_still_be_used(footage):
    got = TR.music_fit({"seconds": 20.0, "phrase_points": [4.0, 12.0]}, bpm_min=None, bpm_max=None, seconds=60.0, energy="")
    assert got["fits"] is True and any("looped or ended early" in n for n in got["notes"])
    assert TR.music_fit({"seconds": 20.0, "phrase_points": []}, bpm_min=None, bpm_max=None, seconds=60.0, energy="")["fits"] is False


def test_the_music_tool_returns_the_profile_and_a_fit(footage, monkeypatch, tmp_path):
    music_env(monkeypatch, footage, tmp_path)
    head, r = call("analyze_music_tool", file_id="M1", bpm_min=100, bpm_max=120, energy="building")
    assert "110 BPM" in head and r["profile"]["bpm"] == 110.0 and r["fit"]["fits"] is False and "description" not in r


def test_describing_music_calls_the_cloud_once_and_the_reading_is_labelled_inferred(footage, monkeypatch, tmp_path):
    music_env(monkeypatch, footage, tmp_path)
    calls = []
    monkeypatch.setattr(audio_cloud, "describe_music", lambda *a, **k: calls.append(1) or {"windows": [{"start": 0, "end": 10, "genre": "ambient"}], "cached": False})
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: object())
    monkeypatch.setattr("classes.media_index.probe.probe_media", lambda p: {"has_audio": True})
    _, r = call("analyze_music_tool", file_id="M1", describe=True)
    assert r["description"][0]["genre"] == "ambient" and "inferred" in r["description_kind"] and calls == [1]


@pytest.mark.parametrize("reply,fragment", [({"auth": True, "error": "x"}, "sign in"), ({"unsupported": True, "error": "x"}, "no media index v2"), ({"error": "quota"}, "quota")])
def test_music_description_failures_become_actionable_errors(footage, monkeypatch, tmp_path, reply, fragment):
    music_env(monkeypatch, footage, tmp_path)
    monkeypatch.setattr(audio_cloud, "describe_music", lambda *a, **k: reply)
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: object())
    monkeypatch.setattr("classes.media_index.probe.probe_media", lambda p: {"has_audio": True})
    out = REGISTRY["analyze_music_tool"].func(file_id="M1", describe=True)
    assert out.startswith("Error") and fragment in out


def test_an_unanalysed_file_is_an_error(footage, monkeypatch, tmp_path):
    music_env(monkeypatch, footage, tmp_path)
    monkeypatch.setattr(TR, "_index_for", lambda f: None)
    assert "no audio analysis" in REGISTRY["analyze_music_tool"].func(file_id="M1")


def test_a_kept_description_is_shown_without_asking_the_cloud_again(footage, monkeypatch, tmp_path):
    music_env(monkeypatch, footage, tmp_path)
    footage[2].music_desc = [{"start": 0, "end": 10, "genre": "lo-fi"}]
    monkeypatch.setattr(audio_cloud, "describe_music", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    _, r = call("analyze_music_tool", file_id="M1")
    assert r["description"][0]["genre"] == "lo-fi"


# ============================ listen_tool ============================
@pytest.fixture(scope="module")
def tone_file(tmp_path_factory):
    ff = mf.need_ffmpeg()
    path = str(tmp_path_factory.mktemp("ln") / "tone.wav")
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=12", path], check=True)
    return path


HEARD = {"overall": {"dialogue_clarity": 0.4}, "findings": [{"start": 2.0, "end": 5.0, "kind": "dialogue_buried", "severity": "high", "note": "buried"},
                                                              {"start": 8.0, "end": 9.0, "kind": "good", "severity": "low", "note": "nice"}], "summary": "Voice is buried.", "usage": {}}


def test_listening_to_a_file_prepares_small_audio_and_returns_the_findings_as_inferred(tone_file, monkeypatch):
    f = SimpleNamespace(id="F1", data={"name": "tone.wav", "path": tone_file, "duration": 12.0})
    monkeypatch.setattr(TR, "resolve_files", lambda ids, query="": [f])
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    seen = {}
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: object())
    monkeypatch.setattr(audio_cloud, "listen", lambda client, path, length, context, **k: seen.update(path=path, length=length, context=context, size=os.path.getsize(path)) or HEARD)
    head, r = call("listen_tool", file_id="F1", start=1.0, end=11.0, form="vlog", vibe="warm")
    assert seen["length"] == 10.0 and seen["context"] == {"form": "vlog", "vibe": "warm"} and seen["path"].endswith("audio.aac") and 0 < seen["size"] < 100000
    assert not os.path.exists(seen["path"]), "the temporary audio is removed"
    assert "1 problem" in head and r["kind"] == "inferred" and "fine loudness balance" in r["reliability"]
    assert r["findings"][0]["start_in_timeline"] == 2.0, "for a file, times are in the file's own seconds from the start of the range"


def test_listening_to_the_timeline_renders_the_mix_and_tells_the_listener_where_speech_is(tone_file, monkeypatch, tmp_path):
    clips = [clip("pic", 0, 12), clip("talk", 2, 6, kind="audio", role="speech"), clip("bed", 0, 12, kind="audio", role="music", layer=2)]
    monkeypatch.setattr(TR, "build_timeline", lambda: (clips, R.ProjectInfo(12.0), {}))
    rendered = tmp_path / "mixdir"
    rendered.mkdir()
    mp3 = rendered / "mix.mp3"
    mp3.write_bytes(open(tone_file, "rb").read())
    asked = []
    monkeypatch.setattr(TR, "render_timeline_mix", lambda lo, hi: asked.append((lo, hi)) or (str(mp3), ""))
    seen = {}
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: object())
    monkeypatch.setattr(audio_cloud, "listen", lambda client, path, length, context, **k: seen.update(length=length, context=context) or HEARD)
    _, r = call("listen_tool", start=1.0, end=10.0, form="YouTube vlog")
    assert asked == [(1.0, 10.0)] and seen["length"] == 9.0
    assert seen["context"]["speech_ranges"] == [[1.0, 5.0]], "speech 2-6 s on the timeline is 1-5 s into the listened range"
    assert r["findings"][0]["start_in_timeline"] == 3.0, "findings are mapped back to timeline seconds"
    assert not rendered.exists()


@pytest.mark.parametrize("setup,fragment", [("no_audio", "no audio"), ("too_short", "too short"), ("render_fails", "could not render")])
def test_timeline_listening_refuses_when_there_is_nothing_to_hear(monkeypatch, setup, fragment):
    clips = [clip("pic", 0, 12)] if setup == "no_audio" else [clip("pic", 0, 12), clip("bed", 0, 12, kind="audio", role="music", layer=2)]
    monkeypatch.setattr(TR, "build_timeline", lambda: (clips, R.ProjectInfo(12.0), {}))
    monkeypatch.setattr(TR, "render_timeline_mix", lambda lo, hi: (None, "an export is already running"))
    args = {"start": 1.0, "end": 2.0} if setup == "too_short" else {}
    out = REGISTRY["listen_tool"].func(**args)
    assert out.startswith("Error") and fragment in out


@pytest.mark.parametrize("reply,fragment", [({"auth": True, "error": "x"}, "sign in"), ({"unsupported": True, "error": "x"}, "no media index v2"), ({"error": "quota"}, "quota")])
def test_listening_failures_become_actionable_errors(tone_file, monkeypatch, reply, fragment):
    f = SimpleNamespace(id="F1", data={"name": "tone.wav", "path": tone_file, "duration": 12.0})
    monkeypatch.setattr(TR, "resolve_files", lambda ids, query="": [f])
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: object())
    monkeypatch.setattr(audio_cloud, "listen", lambda *a, **k: reply)
    out = REGISTRY["listen_tool"].func(file_id="F1")
    assert out.startswith("Error") and fragment in out


def test_a_missing_file_cannot_be_listened_to(monkeypatch):
    f = SimpleNamespace(id="F1", data={"name": "gone.wav", "path": "/nowhere/gone.wav", "duration": 12.0})
    monkeypatch.setattr(TR, "resolve_files", lambda ids, query="": [f])
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    assert "missing on disk" in REGISTRY["listen_tool"].func(file_id="F1")
