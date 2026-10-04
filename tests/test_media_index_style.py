"""The style of an edit in numbers, and where a timeline differs from a reference."""

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
from classes.media_index import style as S  # noqa: E402
from classes.media_index.library import FileIndex  # noqa: E402
from classes.media_index.review import TimelineClip  # noqa: E402


def shot(i, a, b, kind="hard", motion="static", shot_type=None, text=False, black=False):
    return {"id": i, "start": a, "end": b, "black": black, "opens_with": ({"kind": kind} if i else None), "motion": {"class": motion},
            "watch": ({"shot_type": shot_type, "on_screen_text": ([{"text": "HI"}] if text else [])} if shot_type or text else None)}


def reference(**over):
    shots = [shot(0, 0, 4), shot(1, 4, 7, "dissolve", "pan", "wide"), shot(2, 7, 9, "hard", "handheld", "close", text=True), shot(3, 9, 10.5, "hard", "handheld", "close"),
             shot(4, 10.5, 11.5, "fade", "static", "medium"), shot(5, 11.5, 12.0, "hard", "static", "close")]
    base = dict(sha="a" * 64, duration=12.0, orientation="landscape", shots=shots, sentences=[{"start": 0.5, "end": 3.5, "text": "one two three four five six"}],
                audio={"tempo": {"bpm": 120.0, "beats": [i * 0.5 for i in range(24)]}, "loudness": {"integrated_lufs": -14.0},
                       "music": {"sections": [{"label": "intro", "start": 0.0, "end": 6.0}, {"label": "peak", "start": 6.0, "end": 12.0}]}},
                look_file={"profile": {"avg_luma": 0.45, "warm_cool": 0.2, "sat_proxy": 0.5, "contrast_span": 0.6, "palette": ["#aa5522"]}}, pipeline={"hdr": False},
                layers={"watch": True})
    base.update(over)
    return FileIndex(**base)


def accelerating(**over):
    """Thirty seconds that cut slowly, then faster, then fast, with some dissolves and a fade, cutting on a half-second grid."""
    cuts = [0.0, 5.0, 12.0, 16.0, 19.0, 21.0, 22.5, 24.0, 25.5, 27.0, 28.5, 30.0]
    kinds = {1: "dissolve", 3: "dissolve", 5: "dissolve", 7: "fade"}
    shots = [shot(i, cuts[i], cuts[i + 1], kinds.get(i, "hard")) for i in range(len(cuts) - 1)]
    audio = {"tempo": {"bpm": 120.0, "beats": [i * 0.5 for i in range(70)]}, "loudness": {"integrated_lufs": -14.0}}
    return reference(shots=shots, duration=30.0, **{"audio": audio, **over})


def clip(cid, start, end, kind="video"):
    return TimelineClip(id=cid, name=cid, layer=1, kind=kind, start=start, end=end)


# ============================ pacing ============================
def test_the_pacing_curve_counts_cuts_per_minute_in_each_window():
    curve = S.pacing_curve([1.0, 2.0, 3.0, 11.0, 15.0], 20.0, 10.0)
    assert curve == [{"from": 0.0, "to": 10.0, "cuts_per_minute": 18.0}, {"from": 10.0, "to": 20.0, "cuts_per_minute": 12.0}]
    assert S.pacing_curve([], 0.0) == [] and S.pacing_curve([5.0], 4.0, 10.0)[0]["cuts_per_minute"] == 0.0


def test_a_sliver_at_the_end_joins_the_last_window_instead_of_reading_as_a_stop():
    curve = S.pacing_curve([2.0, 12.0, 14.0, 21.0], 21.5, 10.0)
    assert [(c["from"], c["to"]) for c in curve] == [(0.0, 10.0), (10.0, 21.5)], "the last half second is not its own window"
    assert curve[1]["cuts_per_minute"] == round(3 * 60.0 / 11.5, 1)
    assert [(c["from"], c["to"]) for c in S.pacing_curve([], 26.0, 10.0)] == [(0.0, 10.0), (10.0, 20.0), (20.0, 26.0)], "a six second window is kept"


@pytest.mark.parametrize("per_window,shape", [([2, 4, 8], "accelerating"), ([8, 4, 2], "decelerating"), ([4, 4, 4], "steady"), ([4, 5, 4], "steady"), ([3, 3], "steady"), ([2, 8, 2], "steady")])
def test_the_shape_says_whether_an_edit_speeds_up(per_window, shape):
    curve = [{"from": 10.0 * i, "to": 10.0 * i + 10, "cuts_per_minute": n * 6.0} for i, n in enumerate(per_window)]
    assert S.pace_shape(curve) == shape


def test_a_reference_is_read_for_its_pacing_transitions_motion_and_shot_types():
    out = S.edit_style(reference())
    p = out["pacing"]
    assert out["shots"] == 6 and p["shortest"] == 0.5 and p["longest"] == 4.0 and p["average_shot"] == 2.0 and p["cuts_per_minute"] == 25.0
    assert out["transitions"] == {"dissolve": 1, "hard": 3, "fade": 1}
    assert out["motion"]["static"] == pytest.approx(0.458, abs=0.002) and out["motion"]["pan"] == pytest.approx(0.25, abs=0.002) and out["motion"]["handheld"] == pytest.approx(0.292, abs=0.002)
    assert list(out["motion"]) == ["static", "handheld", "pan"], "largest share first"
    assert out["shot_types"] == {"close": 0.5, "wide": 0.375, "medium": 0.125}, "shares of the time that has a described shot type"


def test_a_longer_reference_that_cuts_faster_and_faster_is_accelerating():
    out = S.edit_style(accelerating())
    assert out["pacing"]["shape"] == "accelerating" and [c["cuts_per_minute"] for c in out["pacing"]["curve"]] == [6.0, 18.0, 36.0]


def test_black_shots_are_not_counted_as_shots_and_a_split_is_not_a_transition():
    shots = [shot(0, 0, 4), shot(1, 4, 5, black=True), shot(2, 5, 9, "hard")]
    shots.append({**shot(3, 9, 12), "opens_with": {"kind": "split"}})
    out = S.edit_style(reference(shots=shots, duration=12.0))
    assert out["shots"] == 3 and out["transitions"] == {"hard": 1}
    assert out["pacing"]["average_shot"] == 3.67 and out["pacing"]["shortest"] == 3.0, "the 1 s of black is not a shot"


def test_cuts_on_the_beat_music_speech_sound_look_and_text_are_read():
    out = S.edit_style(reference())
    assert out["music"]["bpm"] == 120.0 and out["music"]["cut_on_beat"] == 1.0 and out["music"]["sections"][1] == {"label": "peak", "from": 6.0, "to": 12.0}
    assert out["speech"]["share"] == 0.25 and out["speech"]["words_per_minute"] == 120.0 and out["sound"]["loudness_lufs"] == -14.0
    assert out["look"]["warm_cool"] == 0.2 and out["look"]["hdr"] is False and out["look"]["palette"] == ["#aa5522"]
    assert out["text_on_screen"] == {"shots_with_text": pytest.approx(1 / 6, abs=0.001), "seen": True}


def test_cuts_off_the_beat_are_measured_as_such():
    off = reference(audio={"tempo": {"bpm": 120.0, "beats": [i * 0.5 + 0.25 for i in range(24)]}})
    assert S.edit_style(off)["music"]["cut_on_beat"] == 0.0
    assert S.edit_style(reference(audio={}))["music"]["cut_on_beat"] is None


def test_a_file_with_one_shot_still_has_a_style_and_no_division_by_zero():
    out = S.edit_style(reference(shots=[shot(0, 0, 5)], duration=5.0, sentences=[]))
    assert out["pacing"]["cuts_per_minute"] == 0.0 and out["speech"] == {"share": 0.0, "words_per_minute": None} and out["transitions"] == {}


# ============================ the timeline ============================
def test_the_timeline_is_measured_the_same_way():
    clips = [clip("a", 0, 6), clip("b", 6, 12), clip("c", 12, 18), clip("pad", 20, 20.0, "video"), clip("song", 0, 18, "audio")]
    out = S.timeline_style([c for c in clips if c.kind == "video"], [i * 0.5 for i in range(40)], 18.0, transitions=2)
    assert out["shots"] == 3 and out["pacing"]["average_shot"] == 6.0 and out["pacing"]["cuts_per_minute"] == 6.7 and out["transitions"] == {"count": 2}
    assert out["music"] == {"cut_on_beat": 1.0, "has_beats": True}
    assert S.timeline_style([], [], 0.0)["shots"] == 0


# ============================ the differences ============================
def style_pair(**yours_over):
    ref = S.edit_style(accelerating())
    yours = {"pacing": {"average_shot": 2.0, "shape": "accelerating"}, "transitions": {"count": 2}, "music": {"cut_on_beat": 0.9},
             "sound": {"loudness_lufs": -14.0}, "look": dict(ref["look"]), "text_on_screen": {"shots_with_text": 0.2}}
    yours.update(yours_over)
    return ref, yours


def aspects(gaps):
    return [g["aspect"] for g in gaps]


def test_a_timeline_that_matches_the_reference_has_nothing_to_fix():
    assert S.compare_style(*style_pair()) == []


def test_slow_cutting_is_told_to_cut_faster_with_the_tools_that_do_it():
    gaps = S.compare_style(*style_pair(pacing={"average_shot": 5.0, "shape": "accelerating"}))
    assert aspects(gaps) == ["pacing"] and "1.8x" in gaps[0]["gap"] and "sync_cuts_to_beats_tool" in gaps[0]["tools"]


def test_cutting_much_faster_than_the_reference_is_told_to_slow_down():
    gaps = S.compare_style(*style_pair(pacing={"average_shot": 1.0, "shape": "accelerating"}))
    assert aspects(gaps) == ["pacing"] and "breathe" in gaps[0]["advice"]


def test_a_reference_that_speeds_up_when_the_timeline_does_not():
    gaps = S.compare_style(*style_pair(pacing={"average_shot": 2.0, "shape": "steady"}))
    assert aspects(gaps) == ["pace shape"] and "toward the end" in gaps[0]["advice"]


def test_cutting_to_the_music_when_the_reference_does_and_the_timeline_does_not():
    gaps = S.compare_style(*style_pair(music={"cut_on_beat": 0.1}))
    assert aspects(gaps) == ["cuts on the beat"] and set(gaps[0]["tools"]) == {"audition_music_tool", "sync_cuts_to_beats_tool"}
    assert aspects(S.compare_style(*style_pair(music={"cut_on_beat": None}))) == ["cuts on the beat"]


def test_a_reference_that_does_not_cut_to_the_beat_asks_nothing_of_the_timeline():
    ref, yours = style_pair(music={"cut_on_beat": 0.0})
    ref["music"]["cut_on_beat"] = 0.2
    assert "cuts on the beat" not in aspects(S.compare_style(ref, yours))


def test_blends_in_the_reference_but_none_in_the_timeline():
    gaps = S.compare_style(*style_pair(transitions={"count": 0}))
    assert aspects(gaps) == ["transitions"] and "dissolves or fades" in gaps[0]["advice"]


def test_loudness_and_look_gaps_name_the_tools_and_small_ones_are_ignored():
    ref, yours = style_pair(sound={"loudness_lufs": -20.0})
    yours["look"]["warm_cool"] = -0.2
    gaps = S.compare_style(ref, yours)
    assert aspects(gaps) == ["loudness", "look: warm_cool"] and gaps[0]["tools"] == ["balance_mix_tool"] and "match_reference_tool" in gaps[1]["tools"]
    close, _ = style_pair(sound={"loudness_lufs": -15.5})
    assert S.compare_style(close, style_pair(sound={"loudness_lufs": -15.5})[1]) == []


def test_text_on_screen_in_the_reference_when_the_timeline_has_none():
    ref = S.edit_style(reference(shots=[shot(0, 0, 4, shot_type="close", text=True), shot(1, 4, 8, "hard", shot_type="close", text=True)], duration=8.0, audio={}))
    gaps = S.compare_style(ref, {"pacing": {}, "transitions": {"count": 0}, "music": {}, "sound": {}, "look": {}, "text_on_screen": {"shots_with_text": 0.0}})
    assert aspects(gaps) == ["text on screen"] and gaps[0]["tools"] == ["add_captions_tool"]


def test_every_tool_the_advice_names_exists():
    from classes import tool_handlers
    ref, yours = style_pair(pacing={"average_shot": 5.0, "shape": "steady"}, music={"cut_on_beat": 0.0}, transitions={"count": 0}, sound={"loudness_lufs": -25.0},
                            text_on_screen={"shots_with_text": 0.0})
    yours["look"]["avg_luma"] = 0.9
    ref["text_on_screen"]["shots_with_text"] = 0.5
    named = {t for g in S.compare_style(ref, yours) for t in g["tools"]}
    assert named >= {"sync_cuts_to_beats_tool", "balance_mix_tool", "match_reference_tool", "add_captions_tool", "generate_transition_clip_tool", "audition_music_tool"}
    real = set(REGISTRY) | set(tool_handlers.AGENT_TOOL_HANDLERS)
    assert not named - real, f"advice names tools that do not exist: {sorted(named - real)}"


# ============================ the tool ============================
def call(**kw):
    out = REGISTRY["get_edit_style_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def setup(monkeypatch):
    from classes.editor_tools import media_index_tools_precision as P
    f = SimpleNamespace(id="R1", data={"name": "ref.mp4", "path": "/m/ref.mp4", "media_type": "video", "fingerprint": {"sha256": "a" * 64}})
    state = SimpleNamespace(index=accelerating(), clips=[clip("a", 0, 8), clip("b", 8, 16), clip("c", 16, 24), clip("song", 0, 24, "audio"), clip("title", 3, 5, "title")])
    monkeypatch.setattr(P, "resolve_files", lambda ids=None, query="": [f])
    monkeypatch.setattr(P, "default_shelf", lambda: None)
    monkeypatch.setattr(P, "_index_for", lambda fo, shelf=None: state.index)
    monkeypatch.setattr(P, "_timeline_facts", lambda: (state.clips, {}, [], {}, None))
    monkeypatch.setattr(P, "_timeline_beats", lambda c: ([i * 0.5 for i in range(60)], []))
    return state


def test_the_tool_reports_the_reference_and_the_gaps_in_the_timeline(setup):
    head, r = call(file_ids=["R1"])
    assert r["reference"]["pacing"]["average_shot"] == 2.73 and r["timeline"]["pacing"]["average_shot"] == 8.0 and r["changed"] is False
    assert "pacing" in [g["aspect"] for g in r["gaps"]] and "2.73 s average shot" in head and "way(s) your timeline differs" in head


def test_only_picture_clips_are_shots_on_the_timeline(setup):
    _, r = call(file_ids=["R1"])
    assert r["timeline"]["shots"] == 3, "the song and the title are not shots"


def test_without_a_comparison_only_the_reference_is_read(setup):
    head, r = call(file_ids=["R1"], compare_to_timeline=False)
    assert r["timeline"] is None and r["gaps"] == [] and head.endswith("accelerating pace.")


def test_an_empty_timeline_has_nothing_to_compare(setup):
    setup.clips = []
    _, r = call(file_ids=["R1"])
    assert r["timeline"] is None and r["gaps"] == []


def test_an_unindexed_reference_is_refused(setup):
    setup.index = None
    assert "no shot index" in REGISTRY["get_edit_style_tool"].func(file_ids=["R1"])
