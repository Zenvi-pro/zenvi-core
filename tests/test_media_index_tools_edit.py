"""sync_cuts_to_beats, balance_mix and the edit brief: what they apply, refuse and leave alone."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY, media_index_tools_edit as TE  # noqa: E402
from classes.editor_tools._base import ToolError  # noqa: E402
from classes.media_index import review as R  # noqa: E402
from test_media_index_review import FakeIndex, audio_index, clip  # noqa: E402


def call(name, **kw):
    out = REGISTRY[name].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


class FakeClipObj:
    def __init__(self, c: R.TimelineClip, extra=None):
        self.id = c.id
        self.data = {"id": c.id, "layer": c.layer, "position": c.start, "start": c.src_in, "end": c.src_out, "duration": c.src_out - c.src_in, **(extra or {})}
        self.saves = []

    def save(self):
        self.saves.append(dict(self.data))


@pytest.fixture
def timeline(monkeypatch):
    state = SimpleNamespace(clips=[], objs={}, calls=[], locked=False, bpm=120.0, beats=None, downbeats=None)

    def fake_build():
        return state.clips, R.ProjectInfo(duration=max((c.end for c in state.clips), default=0.0)), state.objs
    monkeypatch.setattr(TE, "build_timeline", fake_build)
    beats = [round(1.0 + 0.5 * i, 3) for i in range(80)]

    def provider():
        idx = {"M": audio_index(-20.0, 120.0, beats)}
        idx["M"].audio["music"] = {"downbeats": beats[::4]}
        return lambda c: idx.get(c.file_id) or FakeIndex()
    monkeypatch.setattr(TE, "_index_provider", provider)
    monkeypatch.setattr(TE, "frame_seconds", lambda: 1.0 / 30.0)
    monkeypatch.setattr(TE, "tolerance", lambda: 1.0 / 60.0)
    monkeypatch.setattr(TE, "snap", lambda t: round(float(t) * 30.0) / 30.0)
    monkeypatch.setattr(TE, "extend_timeline", lambda: state.calls.append("extend"))
    monkeypatch.setattr(TE, "refresh", lambda: state.calls.append("refresh"))

    def fake_unlocked(layers, what=""):
        if state.locked:
            raise ToolError("track 1 is locked")
    monkeypatch.setattr(TE, "require_unlocked", fake_unlocked)
    monkeypatch.setattr("classes.clip_utils.clip_time_bounds", lambda data: (200.0, 6000), raising=False)

    def load(*clips):
        state.clips = list(clips)
        state.objs = {c.id: FakeClipObj(c) for c in clips}
    state.load = load
    state.set_provider = lambda fn: monkeypatch.setattr(TE, "_index_provider", fn)       # scoped to the test
    return state


def music(cid="bed", end=40.0, layer=2):
    return clip(cid, 0.0, end, kind="audio", role="music", layer=layer, file="M", src_in=0.0)


# ============================ sync_cuts_to_beats_tool ============================
def test_cuts_move_onto_the_beat_and_only_the_edges_around_them_change(timeline):
    timeline.load(clip("a", 0.0, 1.9, src_in=10.0), clip("b", 1.9, 5.0, src_in=30.0), music())
    head, r = call("sync_cuts_to_beats_tool")
    assert r["changed"] is True and len(r["moves"]) == 1 and "Moved 1 cut(s)" in head
    a, b = timeline.objs["a"], timeline.objs["b"]
    move = r["moves"][0]
    assert a.saves[-1]["end"] == pytest.approx(move["earlier_source_out"], abs=1e-3) and a.data["start"] == 10.0 and a.data["position"] == 0.0
    assert b.saves[-1]["start"] == pytest.approx(move["later_source_in"], abs=1e-3) and b.data["end"] == pytest.approx(30.0 + 3.1, abs=1e-6)
    assert b.data["position"] == pytest.approx(round(move["later_position"] * 30) / 30, abs=1e-6)
    assert a.data["duration"] == pytest.approx(a.data["end"] - a.data["start"]) and b.data["duration"] == pytest.approx(b.data["end"] - b.data["start"])
    assert timeline.objs["bed"].saves == [] and timeline.calls == ["extend", "refresh"]
    assert r["music_clip_id"] == "bed" and r["bpm"] == 120.0


def test_a_dry_run_reports_the_plan_and_changes_nothing(timeline):
    timeline.load(clip("a", 0.0, 1.9, src_in=10.0), clip("b", 1.9, 5.0, src_in=30.0), music())
    head, r = call("sync_cuts_to_beats_tool", dry_run=True)
    assert head.startswith("Would move 1 cut") and r["changed"] is False and r["dry_run"] is True and len(r["moves"]) == 1
    assert all(o.saves == [] for o in timeline.objs.values()) and timeline.calls == []


def test_cuts_already_on_the_beat_change_nothing(timeline):
    timeline.load(clip("a", 0.0, 2.0, src_in=10.0), clip("b", 2.0, 5.0, src_in=30.0), music())
    head, r = call("sync_cuts_to_beats_tool")
    assert r["changed"] is False and r["already_on_beat"] == 1 and "Nothing to move" in head and timeline.calls == []


def test_both_cuts_around_a_clip_are_written_once_with_the_final_values(timeline):
    timeline.load(clip("a", 0.0, 1.9, src_in=10.0), clip("b", 1.9, 2.7, src_in=30.0), clip("c", 2.7, 6.0, src_in=50.0), music())
    _, r = call("sync_cuts_to_beats_tool")
    assert len(r["moves"]) == 2
    b = timeline.objs["b"]
    assert len(b.saves) == 1, "one write per clip, with both edges moved"
    assert b.data["position"] == pytest.approx(2.0, abs=0.04) and b.data["position"] + b.data["duration"] == pytest.approx(2.5, abs=0.04)


def test_the_music_is_found_by_itself_or_named_and_must_be_music_with_a_tempo(timeline):
    timeline.load(clip("a", 0.0, 1.9), clip("b", 1.9, 5.0, src_in=30.0), clip("talk", 0.0, 5.0, kind="audio", role="speech", file="S", layer=3))
    out = REGISTRY["sync_cuts_to_beats_tool"].func()
    assert out.startswith("Error") and "no music with a detected tempo" in out
    timeline.load(clip("a", 0.0, 1.9), clip("b", 1.9, 5.0, src_in=30.0), music())
    out = REGISTRY["sync_cuts_to_beats_tool"].func(music_clip_id="a")
    assert out.startswith("Error") and "is not a music clip" in out
    timeline.load(clip("a", 0.0, 1.9), clip("b", 1.9, 5.0, src_in=30.0), clip("bed", 0.0, 40.0, kind="audio", role="music", layer=2, file="NOTEMPO"))
    assert "no steady tempo" in REGISTRY["sync_cuts_to_beats_tool"].func(music_clip_id="bed")


def test_the_longest_music_clip_with_a_tempo_is_the_one_used(timeline):
    timeline.load(clip("a", 0.0, 1.9, src_in=10.0), clip("b", 1.9, 5.0, src_in=30.0), music("short", end=3.0, layer=2), music("long", end=40.0, layer=3))
    _, r = call("sync_cuts_to_beats_tool", dry_run=True)
    assert r["music_clip_id"] == "long"


def test_downbeat_mode_only_uses_the_first_beat_of_each_bar(timeline):
    timeline.load(clip("a", 0.0, 2.45, src_in=10.0), clip("b", 2.45, 8.0, src_in=30.0), music())
    _, every = call("sync_cuts_to_beats_tool", dry_run=True, on="beat")
    _, bars = call("sync_cuts_to_beats_tool", dry_run=True, on="downbeat", max_shift_seconds=0.6)
    assert every["moves"][0]["to"] == pytest.approx(2.5, abs=0.04), "the nearest beat"
    assert bars["moves"][0]["to"] == pytest.approx(3.0, abs=0.04) or bars["moves"][0]["to"] == pytest.approx(1.0, abs=0.04), "only bar starts (1, 3, 5...)"
    assert bars["on"] == "downbeat"


def test_a_locked_track_refuses_before_anything_is_written(timeline):
    timeline.load(clip("a", 0.0, 1.9, src_in=10.0), clip("b", 1.9, 5.0, src_in=30.0), music())
    timeline.locked = True
    out = REGISTRY["sync_cuts_to_beats_tool"].func()
    assert out.startswith("Error") and "locked" in out
    assert all(o.saves == [] for o in timeline.objs.values()) and timeline.calls == []


def test_speaking_clips_are_left_alone_unless_asked(timeline):
    timeline.load(clip("a", 0.0, 1.9, src_in=10.0, role="speech", file="S"), clip("b", 1.9, 5.0, src_in=30.0), music())
    _, r = call("sync_cuts_to_beats_tool")
    assert r["changed"] is False and "mid-word" in r["skipped"][0]["why"]
    _, r2 = call("sync_cuts_to_beats_tool", include_speech=True)
    assert r2["changed"] is True


def test_the_shift_limit_is_honoured(timeline):
    timeline.load(clip("a", 0.0, 1.78, src_in=10.0), clip("b", 1.78, 5.0, src_in=30.0), music())
    assert call("sync_cuts_to_beats_tool", max_shift_seconds=0.1)[1]["changed"] is False
    assert call("sync_cuts_to_beats_tool", max_shift_seconds=0.3)[1]["changed"] is True


def test_titles_and_stills_without_source_limits_are_handled(timeline):
    timeline.load(clip("a", 0.0, 1.9, kind="image", src_in=0.0), clip("b", 1.9, 5.0, src_in=30.0), clip("t", 0.0, 3.0, kind="title", layer=5), music())
    _, r = call("sync_cuts_to_beats_tool")
    assert r["changed"] is True and timeline.objs["t"].saves == []


# ============================ balance_mix_tool ============================
@pytest.fixture
def mixer(monkeypatch, timeline):
    state = SimpleNamespace(volumes=[], ducks=[], renders=[], loudness=[], handler_errors=set(), tl=timeline)

    def set_clip_volume(timeline_clip_id="", level_db="", mode="", **kw):
        state.volumes.append((timeline_clip_id, float(level_db), mode))
        return "Error: locked" if timeline_clip_id in state.handler_errors else "ok"

    def duck_under_speech(**kw):
        state.ducks.append(kw)
        return "Ducked 1 bed under 2 speech window(s)."
    handlers = SimpleNamespace(set_clip_volume=set_clip_volume, duck_under_speech=duck_under_speech)
    monkeypatch.setattr(TE, "th", lambda: handlers)
    monkeypatch.setattr(TE, "on_main", lambda func, *a, **k: func(*a))

    def render(start, end):
        state.renders.append((start, end))
        return "/nowhere/mix.mp3", ""
    monkeypatch.setattr(TE, "render_timeline_mix", render)
    from classes.media_index import audio as au
    monkeypatch.setattr(au, "measure_loudness", lambda path: state.loudness.pop(0) if state.loudness else None)
    monkeypatch.setattr(TE.os, "remove", lambda p: None)
    monkeypatch.setattr(TE.os, "rmdir", lambda p: None)
    return state


def voices_and_music(timeline, levels=(-18.0, -24.0, -20.0)):
    idx = {f"S{i}": audio_index(lv) for i, lv in enumerate(levels)}
    clips = [clip("pic", 0.0, 30.0)] + [clip(f"talk{i}", i * 8.0, i * 8.0 + 6.0, kind="audio", role="speech", file=f"S{i}", layer=2, gain=0.0) for i in range(len(levels))]
    clips.append(clip("bed", 0.0, 30.0, kind="audio", role="music", file="M", layer=3, gain=0.0))
    timeline.load(*clips)
    base = TE._index_provider()
    timeline.set_provider(lambda: (lambda c: idx.get(c.file_id) or base(c)))
    return idx


def test_voices_are_evened_out_the_music_is_ducked_and_the_mix_is_brought_to_the_target(mixer):
    voices_and_music(mixer.tl)
    mixer.loudness = [{"integrated_lufs": -20.0, "true_peak_db": -9.0}, {"integrated_lufs": -14.2, "true_peak_db": -3.5}]
    head, r = call("balance_mix_tool")
    voice_calls = mixer.volumes[:3]                                      # the voice levelling comes first ...
    master_calls = mixer.volumes[3:]                                     # ... then one overall gain on every audio clip
    by_clip = {cid: db for cid, db, mode in voice_calls if mode == "scale"}
    # voices -18, -24 and -20 dB meet where the quietest can reach (+2.28 dB is the 130% ceiling): -21.72
    assert by_clip == {"talk0": pytest.approx(-3.72, abs=0.01), "talk1": pytest.approx(2.28, abs=0.01), "talk2": pytest.approx(-1.72, abs=0.01)}
    assert len(mixer.ducks) == 1 and mixer.ducks[0]["bed_clip_ids"] == "bed" and mixer.ducks[0]["speech_clip_ids"] == "auto"
    assert mixer.ducks[0]["duck_db"] == "-11.7", "voices end at -21.7 dB and the bed is -20 dB: it must go 11.7 dB down to sit a margin under them"
    assert r["ducking"][0]["duck_db"] == -11.7 and r["ducking"][0]["limited"] is False
    # the clip nearest the ceiling (talk1, now at +2.28 dB) leaves no room: the mix cannot be made louder by volume alone
    assert master_calls == [] and r["loudness"]["volume_ceiling"] is True and r["loudness"]["delta_db"] == 0.0
    assert r["loudness"]["before"]["integrated_lufs"] == -20.0 and "after" not in r["loudness"] and r["changed"] is True
    assert "130%" in r["loudness"]["peak_warning"] or "maximum volume" in r["loudness"]["reason"]
    assert "Balanced the mix" in head and mixer.renders == [(0.0, 30.0)]


def test_the_master_gain_raises_every_clip_by_the_room_the_clip_nearest_the_ceiling_has(mixer):
    voices_and_music(mixer.tl)
    mixer.loudness = [{"integrated_lufs": -20.0, "true_peak_db": -9.0}, {"integrated_lufs": -17.7, "true_peak_db": -6.7}]
    _, r = call("balance_mix_tool", even_out_voices=False, duck_music=False)
    assert {cid for cid, _db, _m in mixer.volumes} == {"talk0", "talk1", "talk2", "bed"}
    assert all(db == pytest.approx(2.28, abs=0.01) for _c, db, _m in mixer.volumes), "wants +6 dB, the volume can only give +2.28 dB"
    assert r["loudness"]["volume_ceiling"] is True and r["loudness"]["after"]["integrated_lufs"] == -17.7
    mixer.volumes.clear()
    mixer.loudness = [{"integrated_lufs": -16.0, "true_peak_db": -9.0}, {"integrated_lufs": -14.0, "true_peak_db": -7.0}]
    _, ok_run = call("balance_mix_tool", even_out_voices=False, duck_music=False)
    assert all(db == pytest.approx(2.0, abs=0.01) for _c, db, _m in mixer.volumes) and ok_run["loudness"]["volume_ceiling"] is False


def test_a_dry_run_changes_nothing_and_renders_nothing(mixer):
    voices_and_music(mixer.tl)
    head, r = call("balance_mix_tool", dry_run=True)
    assert r["changed"] is False and r["dry_run"] is True and mixer.volumes == [] and mixer.ducks == [] and mixer.renders == []
    assert len(r["voices"]["adjust"]) == 3 and r["target_lufs"] == -14.0 and head.startswith("Would adjust 3 voice(s)")


def test_each_part_can_be_switched_off(mixer):
    voices_and_music(mixer.tl)
    call("balance_mix_tool", even_out_voices=False, duck_music=False, set_loudness=False)
    assert mixer.volumes == [] and mixer.ducks == [] and mixer.renders == []
    call("balance_mix_tool", even_out_voices=False, duck_music=True, set_loudness=False)
    assert len(mixer.ducks) == 1 and mixer.volumes == []


def test_music_alone_is_not_ducked(mixer):
    mixer.tl.load(clip("pic", 0.0, 20.0), clip("bed", 0.0, 20.0, kind="audio", role="music", file="M", layer=3, gain=0.0))
    _, r = call("balance_mix_tool", set_loudness=False)
    assert mixer.ducks == [] and "no speech" in r["ducking"]


def test_the_loudness_target_follows_the_form_or_an_explicit_value(mixer):
    voices_and_music(mixer.tl, levels=(-20.0,))
    _, film = call("balance_mix_tool", form="short film", dry_run=True)
    _, podcast = call("balance_mix_tool", form="podcast", dry_run=True)
    _, custom = call("balance_mix_tool", form="podcast", target_lufs=-18.0, dry_run=True)
    assert (film["target_lufs"], podcast["target_lufs"], custom["target_lufs"]) == (-23.0, -16.0, -18.0)


def test_a_mix_already_on_target_is_left_alone(mixer):
    voices_and_music(mixer.tl, levels=(-20.0, -20.0))
    mixer.loudness = [{"integrated_lufs": -14.3, "true_peak_db": -3.0}]
    head, r = call("balance_mix_tool", duck_music=False)
    assert r["changed"] is False and "needed no changes" in head and mixer.renders == [(0.0, 30.0)] and mixer.volumes == []


def test_peaks_that_leave_no_room_are_reported_and_the_level_is_not_raised(mixer):
    voices_and_music(mixer.tl, levels=(-20.0, -20.0))
    mixer.loudness = [{"integrated_lufs": -22.0, "true_peak_db": -0.8}]
    head, r = call("balance_mix_tool")
    assert r["loudness"]["delta_db"] == 0.0 and "compressor" in r["loudness"]["peak_warning"] and "compressor" in head
    assert not [v for v in mixer.volumes if v[1] > 0]


def test_a_failing_volume_change_is_reported_not_hidden(mixer):
    voices_and_music(mixer.tl)
    mixer.handler_errors = {"talk0"}
    _, r = call("balance_mix_tool", set_loudness=False)
    assert r["errors"] and "talk0" in r["errors"][0]


def test_a_mix_that_cannot_be_rendered_still_balances_the_rest_and_says_so(mixer, monkeypatch):
    voices_and_music(mixer.tl)
    monkeypatch.setattr(TE, "render_timeline_mix", lambda s, e: (None, "an export is already running"))
    head, r = call("balance_mix_tool")
    assert r["errors"] == ["could not render the mix to measure it: an export is already running"] and len(mixer.ducks) == 1
    assert r["loudness"]["delta_db"] == 0.0


def test_a_timeline_without_sound_is_an_error(mixer):
    mixer.tl.load(clip("pic", 0.0, 10.0))
    assert "no audio to balance" in REGISTRY["balance_mix_tool"].func()


# ============================ the edit brief ============================
@pytest.fixture
def project(monkeypatch):
    store = {TE.BRIEF_KEY: {}}
    writes = []

    class Updates:
        def update_untracked(self, key, values):
            writes.append((key, dict(values)))
            store[key[0]].update(values)                       # what the project store does for a dict value

    app = SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: store.get(k, d)), updates=Updates())
    monkeypatch.setattr(TE, "get_app", lambda: app)
    monkeypatch.setattr(TE, "on_main", lambda func, *a, **k: func(*a))
    return SimpleNamespace(store=store, writes=writes)


def test_a_brief_is_saved_with_the_project_without_an_undo_step(project):
    head, r = call("set_edit_brief_tool", brief={"form": "YouTube vlog", "target_seconds": 60, "bans": ["no stock"]})
    assert r["brief"] == {"form": "YouTube vlog", "target_seconds": 60, "bans": ["no stock"]} and "Saved the edit brief (3 item(s))" in head
    assert project.writes[0][0] == ["edit_brief"] and project.store["edit_brief"]["form"] == "YouTube vlog"


def test_later_calls_update_only_the_keys_given_and_null_removes_one(project):
    call("set_edit_brief_tool", brief={"form": "vlog", "vibe": "warm"})
    _, r = call("set_edit_brief_tool", brief={"vibe": "moody", "done": ["intro"]})
    assert r["brief"] == {"form": "vlog", "vibe": "moody", "done": ["intro"]}
    _, r = call("set_edit_brief_tool", brief={"form": None})
    assert r["brief"] == {"vibe": "moody", "done": ["intro"]} and project.store["edit_brief"]["form"] is None, "the project keeps a null; it reads as absent"
    _, got = call("get_edit_brief_tool")
    assert got["brief"] == {"vibe": "moody", "done": ["intro"]}, "a removed key reads as absent"


def test_replace_swaps_the_whole_brief_and_clears_what_is_gone(project):
    call("set_edit_brief_tool", brief={"form": "vlog", "vibe": "warm"})
    _, r = call("set_edit_brief_tool", brief={"form": "film"}, replace=True)
    assert r["brief"] == {"form": "film"} and r["removed"] == ["vibe"] and project.store["edit_brief"]["vibe"] is None


@pytest.mark.parametrize("bad,fragment", [("text", "JSON object"), ({"x": object()}, "plain JSON"), ({"big": "x" * 70000}, "over 64 KB")])
def test_a_bad_brief_is_refused_and_nothing_is_written(project, bad, fragment):
    out = REGISTRY["set_edit_brief_tool"].func(brief=bad)
    assert out.startswith("Error") and fragment in out and project.writes == []


def test_the_merged_brief_may_not_grow_past_the_limit(project):
    call("set_edit_brief_tool", brief={"a": "x" * 40000})
    out = REGISTRY["set_edit_brief_tool"].func(brief={"b": "y" * 40000})
    assert out.startswith("Error") and "would be over" in out and project.store["edit_brief"].get("b") is None


def test_reading_when_nothing_is_saved_says_so(project):
    head, r = call("get_edit_brief_tool")
    assert r["brief"] == {} and "No edit brief" in head


# ============================ harmonize_look_tool ============================
def look_of(luma=0.5, warm=0.0):
    return {"present": True, "avg_luma": luma, "warm_cool": warm, "green_magenta": 0.0, "sat_proxy": 0.3, "contrast_span": 0.5,
            "clipped_highlights": 0.0, "clipped_shadows": 0.0, "channel_means": {"red": luma, "green": luma, "blue": luma}}


@pytest.fixture
def grader(monkeypatch, timeline):
    state = SimpleNamespace(calls=[], looks={}, after={}, measure_calls=[], result="Matched 2 clip(s)", tl=timeline)

    def measure(clips, objs, *, max_clips, **kw):
        ids = [c.id for c in clips]
        state.measure_calls.append((ids, max_clips))
        table = state.after if len(state.measure_calls) > 1 and state.after else state.looks
        return {i: table[i] for i in ids if i in table}, len(ids)
    monkeypatch.setattr(TE, "measure_looks", measure)

    def match(clipIds="", referenceClipId="", **kw):
        state.calls.append((clipIds, referenceClipId))
        return state.result
    monkeypatch.setattr(TE, "th", lambda: SimpleNamespace(match_color_to_reference=match))
    return state


def picture_edit(timeline, n=5):
    timeline.load(*[clip(f"c{i}", i * 4.0, i * 4.0 + 4.0, src_in=i * 50.0, name=f"clip {i}") for i in range(n)])


def test_strays_are_matched_to_the_median_clip_and_reported_before_and_after(grader):
    picture_edit(grader.tl)
    grader.looks = {"c0": look_of(0.50), "c1": look_of(0.52), "c2": look_of(0.48), "c3": look_of(0.20, -0.2), "c4": look_of(0.85)}
    grader.after = {"c3": look_of(0.48, -0.01), "c4": look_of(0.53)}
    head, r = call("harmonize_look_tool")
    assert r["reference"] in ("c0", "c1", "c2") and r["to_match"] == ["c3", "c4"], "the dark, cool clip is furthest from the reference, so it goes first"
    clip_ids, ref = grader.calls[0]
    assert set(clip_ids.split(",")) == {"c3", "c4"} and ref == r["reference"]
    assert r["changed"] is True and set(r["improved"]) == {"c3", "c4"} and all(r["after"][c] < r["distances"][c] for c in ("c3", "c4"))
    assert r["still_far"] == [] and "still differ" not in head
    assert "Matched 2 clip(s)" in head and "now look closer" in head


def test_a_dry_run_measures_and_reports_without_matching(grader):
    picture_edit(grader.tl)
    grader.looks = {f"c{i}": look_of(0.5 if i < 4 else 0.9) for i in range(5)}
    head, r = call("harmonize_look_tool", dry_run=True)
    assert r["changed"] is False and r["dry_run"] is True and r["to_match"] == ["c4"] and grader.calls == [] and head.startswith("Would match 1 clip")


def test_a_consistent_edit_changes_nothing(grader):
    picture_edit(grader.tl)
    grader.looks = {f"c{i}": look_of(0.5 + 0.005 * i) for i in range(5)}
    head, r = call("harmonize_look_tool")
    assert r["changed"] is False and grader.calls == [] and "already look alike" in head


def test_a_named_clip_is_the_reference_and_an_unknown_one_is_an_error(grader):
    picture_edit(grader.tl)
    grader.looks = {f"c{i}": look_of(0.5 if i < 4 else 0.9) for i in range(5)}
    _, r = call("harmonize_look_tool", reference="c4", dry_run=True)
    assert r["reference"] == "c4" and sorted(r["to_match"]) == ["c0", "c1", "c2", "c3"]
    out = REGISTRY["harmonize_look_tool"].func(reference="ghost")
    assert out.startswith("Error") and "no measured look" in out


def test_the_tolerance_and_limit_are_honoured(grader):
    picture_edit(grader.tl, n=6)
    grader.looks = {f"c{i}": look_of(0.5 if i < 3 else 0.5 + 0.04 * (i - 2)) for i in range(6)}
    _, loose = call("harmonize_look_tool", tolerance=0.9, dry_run=True)
    assert loose["to_match"] == []
    _, limited = call("harmonize_look_tool", tolerance=0.05, max_clips=1, dry_run=True)
    assert len(limited["to_match"]) == 1 and len(limited["left_out"]) >= 1 and limited["distances"][limited["to_match"][0]] >= limited["distances"][limited["left_out"][0]]


def test_too_few_clips_or_measurements_are_clear_errors(grader):
    grader.tl.load(clip("only", 0.0, 5.0))
    assert "at least two picture clips" in REGISTRY["harmonize_look_tool"].func()
    picture_edit(grader.tl, n=3)
    grader.looks = {"c0": look_of()}
    assert "fewer than two clips could be measured" in REGISTRY["harmonize_look_tool"].func()


def test_a_failing_match_is_an_error_not_a_success(grader):
    picture_edit(grader.tl)
    grader.looks = {f"c{i}": look_of(0.5 if i < 4 else 0.9) for i in range(5)}
    grader.result = "Error: could not render the reference"
    out = REGISTRY["harmonize_look_tool"].func()
    assert out.startswith("Error") and "colour match failed" in out


def test_only_the_matched_clips_are_measured_again(grader):
    picture_edit(grader.tl)
    grader.looks = {f"c{i}": look_of(0.5 if i < 4 else 0.9) for i in range(5)}
    grader.after = {"c4": look_of(0.5)}
    call("harmonize_look_tool")
    assert grader.measure_calls[0][1] == 24 and grader.measure_calls[1][0] == ["c4"]


# ============================ audition_music_tool ============================
def test_the_audition_ranks_candidates_and_says_how_to_place_the_winner(monkeypatch):
    ranked = [{"id": "11", "name": "Groove", "fits": True, "score": 1.0, "bpm": 120.0, "phrase_points": [4.0, 12.0], "preview_url": "https://cdn.freesound.org/a.mp3", "rank": 1},
              {"id": "22", "name": "Drone", "fits": False, "score": 0.0, "bpm": None, "phrase_points": [], "preview_url": "https://cdn.freesound.org/b.mp3", "rank": 2}]
    seen = {}
    monkeypatch.setattr(TE.audition, "audition", lambda cands, **kw: seen.update(cands=cands, kw=kw) or {"ranked": ranked, "failed": [{"id": "33", "name": "X", "error": "nope"}], "analysed": 3})
    head, r = call("audition_music_tool", candidates=[{"id": "11", "preview_url": "https://cdn.freesound.org/a.mp3"}], bpm_min=100, bpm_max=130, seconds_needed=60, energy="building")
    assert "Best fit: Groove (120 BPM); 1 of 2 fit" in head and r["use_best"] == {"sound_id": "11", "preview_url": "https://cdn.freesound.org/a.mp3", "start_seconds": 4.0}
    assert seen["kw"] == {"bpm_min": 100.0, "bpm_max": 130.0, "seconds": 60.0, "energy": "building"} and r["failed"][0]["id"] == "33"


def test_a_winner_without_phrase_points_starts_at_zero_and_no_wishes_means_no_fit_count(monkeypatch):
    ranked = [{"id": "22", "name": "Drone", "fits": True, "score": 1.0, "bpm": None, "phrase_points": [], "preview_url": "https://cdn.freesound.org/b.mp3", "rank": 1}]
    monkeypatch.setattr(TE.audition, "audition", lambda cands, **kw: {"ranked": ranked, "failed": [], "analysed": 1})
    head, r = call("audition_music_tool", candidates=[{"id": "22", "preview_url": "https://cdn.freesound.org/b.mp3"}])
    assert head == "Best fit: Drone (no steady tempo)." and r["use_best"]["start_seconds"] == 0.0


def test_nothing_usable_is_an_error_that_says_why(monkeypatch):
    monkeypatch.setattr(TE.audition, "audition", lambda cands, **kw: {"ranked": [], "failed": [{"id": "1", "name": "A", "error": "only https preview URLs are fetched"}], "analysed": 1})
    out = REGISTRY["audition_music_tool"].func(candidates=[{"id": "1", "preview_url": "http://x"}])
    assert out.startswith("Error") and "none of the candidates could be analysed" in out and "only https" in out
    assert REGISTRY["audition_music_tool"].func(candidates=[]).startswith("Error")


# ============================ how deep each bed is ducked ============================
def test_each_bed_is_ducked_to_a_margin_under_the_quietest_voice_it_plays_with(mixer):
    voices_and_music(mixer.tl, levels=(-18.0, -26.0))
    _, r = call("balance_mix_tool", even_out_voices=False, set_loudness=False)
    assert mixer.ducks[0]["duck_db"] == "-16.0", "the quietest voice is -26 dB and the bed -20 dB: 16 dB down leaves a 10 dB margin"
    assert r["ducking"][0]["needed"] == -16.0 and r["ducking"][0]["bed"] == "bed"


def test_the_depth_uses_the_voice_levels_as_they_will_be_after_the_voices_are_evened_out(mixer):
    voices_and_music(mixer.tl, levels=(-18.0, -26.0))
    call("balance_mix_tool", set_loudness=False)
    assert mixer.ducks[0]["duck_db"] == "-13.7", "voices meet at -23.7 dB (the quiet one can only be lifted to the 130% ceiling), the bed is -20 dB: 13.7 dB down"


def test_a_bed_already_far_enough_under_the_voice_is_not_ducked(mixer):
    voices_and_music(mixer.tl, levels=(-18.0, -18.0))
    mixer.tl.load(*[c for c in mixer.tl.clips if c.id != "bed"], clip("bed", 0.0, 30.0, kind="audio", role="music", file="M", layer=3, gain=-20.0))
    _, r = call("balance_mix_tool", even_out_voices=False, set_loudness=False)
    assert mixer.ducks == [] and r["ducking"][0]["why"] == "already far enough under the voice" and r["ducking"][0]["duck_db"] is None


def test_unknown_levels_leave_ducking_to_the_editors_own_estimate(mixer):
    idx = {}
    clips = [clip("pic", 0.0, 30.0), clip("talk0", 0.0, 6.0, kind="audio", role="speech", file="NOINDEX", layer=2, gain=0.0),
             clip("bed", 0.0, 30.0, kind="audio", role="music", file="NOINDEX2", layer=3, gain=0.0)]
    mixer.tl.load(*clips)
    call("balance_mix_tool", even_out_voices=False, set_loudness=False)
    assert mixer.ducks and mixer.ducks[0]["duck_db"] == "auto" and idx == {}


def test_two_beds_are_each_given_their_own_depth(mixer):
    voices_and_music(mixer.tl, levels=(-20.0,))
    mixer.tl.load(*mixer.tl.clips, clip("loud", 0.0, 30.0, kind="audio", role="music", file="L", layer=4, gain=0.0))
    base = TE._index_provider()
    mixer.tl.set_provider(lambda: (lambda c: audio_index(-14.0) if c.file_id == "L" else base(c)))
    call("balance_mix_tool", even_out_voices=False, set_loudness=False)
    depths = {d["bed_clip_ids"]: d["duck_db"] for d in mixer.ducks}
    assert depths == {"bed": "-10.0", "loud": "-16.0"}, "the louder bed has to go further down"


def test_a_bed_far_enough_under_stays_untouched_even_when_the_voices_are_adjusted(mixer):
    voices_and_music(mixer.tl, levels=(-18.0, -24.0))
    mixer.tl.load(*[c for c in mixer.tl.clips if c.id != "bed"], clip("bed", 0.0, 30.0, kind="audio", role="music", file="M", layer=3, gain=-20.0))
    _, r = call("balance_mix_tool", set_loudness=False)
    assert [v for v in mixer.volumes if v[0].startswith("talk")], "the voices were evened out"
    assert mixer.ducks == [], "but the bed, already 20 dB down, is left as it is"
    assert r["ducking"][0]["why"] == "already far enough under the voice"


def test_a_clip_that_is_still_far_after_matching_is_reported_with_what_to_try(grader):
    picture_edit(grader.tl)
    grader.looks = {f"c{i}": look_of(0.5 if i < 4 else 0.95) for i in range(5)}
    grader.after = {"c4": look_of(0.85)}               # moved closer, not close enough
    head, r = call("harmonize_look_tool")
    assert r["improved"] == ["c4"] and r["still_far"] == ["c4"] and "still differ by more than 0.15" in head and "run it again" in head


def test_the_clips_are_measured_afresh_after_the_match_not_from_the_objects_read_before(grader, monkeypatch):
    picture_edit(grader.tl)
    grader.looks = {f"c{i}": look_of(0.5 if i < 4 else 0.9) for i in range(5)}
    grader.after = {"c4": look_of(0.5)}
    builds = []
    original = TE.build_timeline
    monkeypatch.setattr(TE, "build_timeline", lambda: builds.append(1) or original())
    call("harmonize_look_tool")
    assert len(builds) == 2, "the timeline is read once to plan and again after the match"
