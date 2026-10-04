"""The audit of a finished edit: every rule on small hand-built timelines."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes.media_index import review as R  # noqa: E402
from classes.media_index.review import Brief, ProjectInfo, TimelineClip  # noqa: E402


def clip(cid, start, end, *, layer=1, kind="video", file="F1", src_in=0.0, speed=1.0, role=None, gain=0.0, audio=False, effects=(), name=None):
    return TimelineClip(id=cid, name=name or cid, file_id=file, sha=file, layer=layer, kind=kind, start=start, end=end, src_in=src_in,
                        src_out=src_in + (end - start) * speed, speed=speed, role=role, gain_db=gain, has_audio=audio or role is not None,
                        effects=list(effects))


class FakeIndex:
    def __init__(self, shots=None, audio=None, pipeline=None):
        self.shots, self.audio, self.pipeline = shots or [], audio or {}, pipeline or {}

    def shot_at(self, t):
        for s in self.shots:
            if s["start"] <= t < s["end"]:
                return s
        return None


def shot(start, end, *, flags=(), highlight=0.7, hook=None, shot_type=None):
    q = {"flags": list(flags), "highlight": highlight, "score": 1.0}
    if hook is not None:
        q["inferred"] = {"hook_potential": hook}
    return {"start": start, "end": end, "quality": q, "watch": {"shot_type": shot_type} if shot_type else {}}


def provider(**by_file):
    return lambda c: by_file.get(c.file_id)


def by_id(result):
    return {f["id"]: f for layer in result["layers"].values() for f in layer}


def run(clips, brief=None, project=None, index=None, **kw):
    return R.review(clips, project or ProjectInfo(duration=max((c.end for c in clips), default=0.0)), brief or Brief(),
                    index_for=index or (lambda c: None), **kw)


# ============================ what the viewer sees ============================
def test_the_topmost_picture_wins_and_runs_of_one_clip_merge():
    base = clip("base", 0, 10, layer=1)
    pip = clip("pip", 3, 5, layer=2)
    shots = R.visible_shots([base, pip, clip("title", 0, 5, kind="title", layer=5), clip("music", 0, 10, kind="audio", role="music", layer=3)])
    assert [(s["clip"].id, s["start"], s["end"]) for s in shots] == [("base", 0.0, 3.0), ("pip", 3.0, 5.0), ("base", 5.0, 10.0)]
    assert R.visible_shots([]) == []
    two = R.visible_shots([clip("a", 0, 4), clip("b", 4, 8)])
    assert [s["clip"].id for s in two] == ["a", "b"]


def test_a_gap_between_clips_is_not_a_shot():
    assert [(s["start"], s["end"]) for s in R.visible_shots([clip("a", 0, 3), clip("b", 5, 8)])] == [(0.0, 3.0), (5.0, 8.0)]


# ============================ story ============================
def test_no_picture_is_the_first_problem():
    assert by_id(run([]))["picture_present"]["status"] == "needs"


@pytest.mark.parametrize("duration,status", [(60, "ok"), (65, "ok"), (55, "ok"), (70, "needs"), (40, "needs")])
def test_length_is_judged_against_the_target_within_ten_percent(duration, status):
    f = by_id(run([clip("a", 0, duration)], Brief(target_seconds=60)))["length"]
    assert f["status"] == status and f["evidence"]["target"] == 60
    assert (f["fix"] == []) == (status == "ok")


def test_no_target_means_no_length_verdict():
    assert "length" not in by_id(run([clip("a", 0, 30)]))


def test_a_short_form_has_a_length_cap():
    assert by_id(run([clip("a", 0, 75)], Brief(form="YouTube Short")))["form_length"]["status"] == "needs"
    assert "form_length" not in by_id(run([clip("a", 0, 75)], Brief(form="vlog")))
    assert "form_length" not in by_id(run([clip("a", 0, 50)], Brief(form="YouTube Short")))


def shots_of(lengths, **kw):
    out, t = [], 0.0
    for i, n in enumerate(lengths):
        out.append(clip(f"c{i}", t, t + n, **kw))
        t += n
    return out


def test_frantic_cutting_is_flagged():
    f = by_id(run(shots_of([0.3] * 4 + [3.0, 2.0, 4.0])))["pacing"]
    assert f["status"] == "needs" and f["evidence"]["share_under_half_second"] > 0.25


def test_every_shot_the_same_length_is_a_gentle_note_not_a_demand():
    f = by_id(run(shots_of([3.0] * 8)))["pacing"]
    assert f["status"] == "info" and "same length" in f["summary"]


def test_a_slow_short_form_is_flagged_but_a_slow_film_is_not():
    slow = shots_of([14.0, 6.0, 22.0, 9.0, 30.0, 11.0])
    assert by_id(run(slow, Brief(form="TikTok")))["pacing"]["status"] == "needs"
    assert by_id(run(slow, Brief(form="short film")))["pacing"]["status"] == "ok"


def test_varied_shot_lengths_are_fine_and_too_few_shots_cannot_be_judged():
    assert by_id(run(shots_of([2.0, 5.0, 1.5, 6.0, 3.0, 2.5, 4.0])))["pacing"]["status"] == "ok"
    assert by_id(run(shots_of([2.0, 5.0])))["pacing"]["status"] == "info"


def test_the_opening_shot_is_judged_by_the_models_hook_score_and_measured_flags():
    idx = FakeIndex([shot(0, 20, hook=0.9)])
    assert by_id(run([clip("a", 0, 5)], index=provider(F1=idx)))["hook"]["status"] == "ok"
    weak = FakeIndex([shot(0, 20, hook=0.1)])
    assert by_id(run([clip("a", 0, 5)], index=provider(F1=weak)))["hook"]["status"] == "needs"
    blurry = FakeIndex([shot(0, 20, hook=0.9, flags=["blurry"])])
    f = by_id(run([clip("a", 0, 5)], index=provider(F1=blurry)))["hook"]
    assert f["status"] == "needs" and f["evidence"]["flags"] == ["blurry"], "a measured defect beats the model's enthusiasm"
    assert by_id(run([clip("a", 0, 5)]))["hook"]["status"] == "unknown"


def test_without_a_hook_score_the_highlight_decides():
    idx = FakeIndex([shot(0, 20, highlight=0.3)])
    assert by_id(run([clip("a", 0, 5)], index=provider(F1=idx)))["hook"]["status"] == "needs"
    assert by_id(run([clip("a", 0, 5)], index=provider(F1=FakeIndex([shot(0, 20, highlight=0.8)])))) ["hook"]["status"] == "ok"


def test_the_hook_looks_at_the_source_time_of_the_first_shot():
    idx = FakeIndex([shot(0, 10, hook=0.1), shot(10, 20, hook=0.9)])
    assert by_id(run([clip("a", 0, 5, src_in=12.0)], index=provider(F1=idx)))["hook"]["status"] == "ok"
    assert by_id(run([clip("a", 0, 5, src_in=2.0)], index=provider(F1=idx)))["hook"]["status"] == "needs"


def test_weak_shots_are_listed_with_where_they_are():
    idx = FakeIndex([shot(0, 10), shot(10, 20, flags=["blurry", "shaky"]), shot(20, 30, flags=["dark"])])
    clips = [clip("a", 0, 5, src_in=0), clip("b", 5, 9, src_in=11), clip("c", 9, 12, src_in=22)]
    f = by_id(run(clips, index=provider(F1=idx)))["weak_shots"]
    assert f["status"] == "needs" and [(r["clip"], r["at"]) for r in f["evidence"]["shots"]] == [("b", 5.0), ("c", 9.0)]
    minor = by_id(run([clip("c", 0, 3, src_in=22)], index=provider(F1=idx)))["weak_shots"]
    assert minor["status"] == "info", "a dark shot alone is a note, not a failure"
    assert by_id(run([clip("a", 0, 5)], index=provider(F1=idx)))["weak_shots"]["status"] == "ok"


def test_the_same_moment_used_twice_is_flagged_but_different_moments_of_one_file_are_not():
    twice = [clip("a", 0, 4, src_in=10), clip("b", 4, 8, src_in=11)]
    f = by_id(run(twice))["repeats"]
    assert f["status"] == "needs" and f["evidence"]["repeated"][0]["clips"] == ["a", "b"]
    assert by_id(run([clip("a", 0, 4, src_in=0), clip("b", 4, 8, src_in=30)]))["repeats"]["status"] == "ok"
    assert by_id(run([clip("a", 0, 4, file="X", src_in=10), clip("b", 4, 8, file="Y", src_in=10)]))["repeats"]["status"] == "ok"


def test_near_duplicate_takes_used_together_are_noted():
    groups = [{"best": {"file_id": "F1"}, "members": [{"file_id": "F1", "shot_id": 0}, {"file_id": "F2", "shot_id": 0}]}]
    f = by_id(run([clip("a", 0, 4, file="F1"), clip("b", 4, 8, file="F2")], take_groups=groups))["repeats"]
    assert f["status"] == "info" and f["evidence"]["similar_groups"] == 1


def test_a_single_shot_type_throughout_is_noted():
    idx = FakeIndex([shot(0, 100, shot_type="wide")])
    f = by_id(run(shots_of([2.0, 3.0, 2.5, 4.0, 3.5, 2.2], src_in=0), index=provider(F1=idx)))["variety"]
    assert f["status"] == "info" and f["evidence"]["top"] == "wide"
    mixed = FakeIndex([shot(0, 6, shot_type="wide"), shot(6, 12, shot_type="close"), shot(12, 100, shot_type="medium")])
    clips = [clip(f"c{i}", i * 3.0, i * 3.0 + 3.0, src_in=i * 3.0 if i < 6 else 13.0) for i in range(8)]
    assert by_id(run(clips, index=provider(F1=mixed)))["variety"]["status"] == "ok"


# ============================ picture ============================
def look(luma=0.5, warm=0.0, **kw):
    return {"present": True, "avg_luma": luma, "warm_cool": warm, "clipped_highlights": 0.0, "clipped_shadows": 0.0, "contrast_span": 0.5, **kw}


def test_clips_that_differ_in_brightness_or_warmth_need_a_consistency_pass():
    clips = shots_of([3.0] * 5)
    even = {c.id: look(0.50 + i * 0.01, 0.02) for i, c in enumerate(clips)}
    assert by_id(run(clips, looks=even))["consistency"]["status"] == "ok"
    bright = dict(even)
    bright["c4"] = look(0.85, 0.02)
    f = by_id(run(clips, looks=bright))["consistency"]
    assert f["status"] == "needs" and [o["clip"] for o in f["evidence"]["outliers"]] == ["c4"] and "harmonize_look_tool" in f["fix"]
    warm = {c.id: look(0.5, 0.0 if i % 2 else 0.2) for i, c in enumerate(clips)}
    assert by_id(run(clips, looks=warm))["consistency"]["status"] == "needs"


def test_a_clip_split_by_an_overlay_counts_once():
    base = clip("base", 0, 12)
    overlay = clip("pip", 4, 6, layer=2)
    looks = {"base": look(0.5), "pip": look(0.5)}
    assert by_id(run([base, overlay], looks=looks))["consistency"]["evidence"]["measured_clips"] == 2
    assert by_id(run([base, clip("title", 4, 6, kind="title", layer=2)], looks={"base": look(0.5)}))["consistency"]["status"] == "unknown", \
        "one clip measured twice would have looked like two"


def test_unmeasured_clips_make_consistency_unknown_not_fine():
    clips = shots_of([3.0] * 3)
    assert by_id(run(clips))["consistency"]["status"] == "unknown"
    assert by_id(run(clips, looks={"c0": look()}))["consistency"]["status"] == "unknown"


def test_exposure_problems_are_listed_per_clip():
    clips = shots_of([3.0] * 3)
    looks = {"c0": look(0.5), "c1": look(0.08), "c2": look(0.5, clipped_highlights=0.2)}
    f = by_id(run(clips, looks=looks))["exposure"]
    assert f["status"] == "needs" and {c["clip"]: c["problems"] for c in f["evidence"]["clips"]} == {"c1": ["dark"], "c2": ["clipped_highlights"]}
    assert by_id(run(clips, looks={c.id: look() for c in clips}))["exposure"]["status"] == "ok"
    assert by_id(run(clips))["exposure"]["status"] == "unknown"


def test_hdr_footage_without_a_grade_is_flagged_and_a_grade_clears_it():
    idx = FakeIndex(pipeline={"hdr": True})
    f = by_id(run([clip("a", 0, 3)], index=provider(F1=idx)))["colour_pipeline"]
    assert f["status"] == "needs" and f["evidence"]["clips"][0]["clip"] == "a"
    assert "colour_pipeline" not in by_id(run([clip("a", 0, 3, effects=["ColorGrade"])], index=provider(F1=idx)))
    assert "colour_pipeline" not in by_id(run([clip("a", 0, 3)], index=provider(F1=FakeIndex())))


def test_a_look_that_was_asked_for_must_be_applied_but_an_ungraded_natural_edit_is_only_a_note():
    plain = [clip("a", 0, 3), clip("b", 3, 6)]
    assert by_id(run(plain, Brief(vibe="warm golden hour")))["style"]["status"] == "needs"
    assert by_id(run(plain, Brief(vibe="warm golden hour", wants_grade=False)))["style"]["status"] == "info"
    assert by_id(run(plain))["style"]["status"] == "info"
    assert by_id(run(plain, Brief(wants_grade=True)))["style"]["status"] == "needs"
    graded = [clip("a", 0, 3, effects=["ColorGrade"]), clip("b", 3, 6)]
    assert by_id(run(graded, Brief(vibe="warm")))["style"]["status"] == "ok"
    assert by_id(run([clip("a", 0, 3, effects=["LUT"])]))["style"]["status"] == "ok"


# ============================ sound ============================
def audio_index(rms_db, bpm=None, beats=None):
    windows = [{"start": float(a), "end": float(a + 5), "rms_db": rms_db} for a in range(0, 100, 5)]
    return FakeIndex(audio={"windows": windows, "tempo": {"bpm": bpm, "beats": beats} if bpm else None})


def test_source_level_is_the_power_average_of_the_audible_windows():
    idx = FakeIndex(audio={"windows": [{"start": 0, "end": 5, "rms_db": -20.0}, {"start": 5, "end": 10, "rms_db": -26.0},
                                       {"start": 10, "end": 15, "rms_db": -80.0}]})
    c = clip("a", 0, 15, role="speech")
    assert R.source_level_db(c, idx) == pytest.approx(-22.04, abs=0.05), "a power average (not -23 dB), and the silent window does not drag it down"
    assert R.source_level_db(c, idx, 5.0, 8.0) == -26.0
    assert R.source_level_db(c, FakeIndex()) is None and R.source_level_db(c, None) is None


def test_music_is_expected_for_a_vlog_not_for_a_podcast_and_the_user_can_override():
    vlog = [clip("v", 0, 30)]
    assert by_id(run(vlog, Brief(form="YouTube vlog")))["music"]["status"] == "needs"
    assert by_id(run(vlog, Brief(form="podcast")))["music"]["status"] == "ok"
    assert by_id(run(vlog, Brief(form="vlog", wants_music=False)))["music"]["status"] == "ok"
    assert by_id(run(vlog, Brief(form="podcast", wants_music=True)))["music"]["status"] == "needs"
    assert by_id(run(vlog))["music"]["status"] == "info", "no form given: a note, not a demand"
    with_music = vlog + [clip("m", 0, 28, kind="audio", role="music", layer=2)]
    assert by_id(run(with_music, Brief(form="vlog")))["music"]["status"] == "ok"
    partial = vlog + [clip("m", 0, 10, kind="audio", role="music", layer=2)]
    f = by_id(run(partial, Brief(form="vlog")))["music"]
    assert f["status"] == "needs" and f["evidence"]["coverage"] == pytest.approx(0.33, abs=0.01)


def mixed(gain_music=0.0, gain_speech=0.0, music_db=-22.0, speech_db=-20.0):
    clips = [clip("pic", 0, 30), clip("talk", 2, 20, kind="audio", role="speech", gain=gain_speech, file="S", layer=2),
             clip("bed", 0, 30, kind="audio", role="music", gain=gain_music, file="M", layer=3)]
    idx = provider(S=audio_index(speech_db), M=audio_index(music_db), F1=FakeIndex())
    return clips, idx


def test_the_voice_must_sit_well_above_the_music_in_the_mix_as_placed():
    clips, idx = mixed(gain_music=0.0)                                   # speech -20, music -22: 2 dB apart
    f = by_id(run(clips, Brief(form="vlog"), index=idx))["dialogue_margin"]
    assert f["status"] == "needs" and f["evidence"]["worst_db"] == pytest.approx(2.0, abs=0.2) and "balance_mix_tool" in f["fix"]
    clips, idx = mixed(gain_music=-15.0)                                 # music pulled down 15 dB: 17 dB apart
    ok = by_id(run(clips, Brief(form="vlog"), index=idx))["dialogue_margin"]
    assert ok["status"] == "ok" and ok["evidence"]["worst_db"] == pytest.approx(17.0, abs=0.2)
    clips, idx = mixed(gain_speech=6.0)                                  # the voice boosted instead
    assert by_id(run(clips, Brief(form="vlog"), index=idx))["dialogue_margin"]["evidence"]["worst_db"] == pytest.approx(8.0, abs=0.2)


def test_the_margin_follows_the_music_source_time_not_the_timeline_time():
    # the bed plays source 20-30 s: its first half (20-25) is loud, its second half (25-30) quiet
    windows = [{"start": 20, "end": 25, "rms_db": -14.0}, {"start": 25, "end": 30, "rms_db": -40.0}]
    bed_index = FakeIndex(audio={"windows": windows})
    clips = [clip("talk", 0, 5, kind="audio", role="speech", file="S"), clip("bed", 0, 10, kind="audio", role="music", file="M", src_in=20.0)]
    f = by_id(run(clips, index=provider(S=audio_index(-20.0), M=bed_index)))["dialogue_margin"]
    assert f["evidence"]["worst_db"] == pytest.approx(-6.0, abs=0.2), "while the voice speaks (0-5 s) the bed plays source 20-25 s, at -14 dB"


def test_unindexed_levels_are_unknown_not_assumed_fine():
    clips, _ = mixed()
    assert by_id(run(clips, Brief(form="vlog"), index=provider(S=FakeIndex(), M=FakeIndex())))["dialogue_margin"]["status"] == "unknown"


def test_voice_level_jumps_between_clips_are_flagged():
    clips = [clip("a", 0, 5, role="speech", file="A"), clip("b", 5, 10, role="speech", file="B")]
    idx = provider(A=audio_index(-18.0), B=audio_index(-30.0))
    f = by_id(run(clips, index=idx))["level_jumps"]
    assert f["status"] == "needs" and f["evidence"]["jumps"][0]["jump_db"] == pytest.approx(-12.0, abs=0.2)
    assert by_id(run(clips, index=provider(A=audio_index(-18.0), B=audio_index(-20.0))))["level_jumps"]["status"] == "ok"
    assert "level_jumps" not in by_id(run(clips[:1], index=idx))


def test_two_voices_at_once_is_flagged():
    clips = [clip("a", 0, 6, role="speech"), clip("b", 4, 9, role="speech")]
    assert by_id(run(clips))["speech_overlap"]["status"] == "needs"
    assert "speech_overlap" not in by_id(run([clip("a", 0, 4, role="speech"), clip("b", 4, 9, role="speech")]))


def test_stretches_with_no_sound_at_all_are_found():
    clips = [clip("pic", 0, 20), clip("talk", 0, 5, role="speech"), clip("amb", 12, 20, kind="audio", role="ambient")]
    f = by_id(run(clips))["dead_air"]
    assert f["status"] == "needs" and f["evidence"]["gaps"] == [[5.0, 12.0]]
    full = [clip("pic", 0, 20), clip("bed", 0, 20, kind="audio", role="music")]
    assert by_id(run(full))["dead_air"]["status"] == "ok"
    tail = [clip("pic", 0, 20), clip("bed", 0, 10, kind="audio", role="music")]
    assert by_id(run(tail))["dead_air"]["evidence"]["gaps"] == [[10.0, 20.0]]


@pytest.mark.parametrize("lufs,peak,form,status", [(-14.2, -2.0, "vlog", "ok"), (-20.0, -2.0, "vlog", "needs"), (-9.0, -2.0, "vlog", "needs"),
                                                   (-14.0, -0.2, "vlog", "needs"), (-16.5, -3.0, "podcast", "ok"), (-14.0, -3.0, "film", "needs"),
                                                   (-23.5, -3.0, "film", "ok")])
def test_the_rendered_mix_is_judged_against_the_platform_target(lufs, peak, form, status):
    f = by_id(run([clip("a", 0, 10)], Brief(form=form), mix={"integrated_lufs": lufs, "true_peak_db": peak, "lra": 5.0}))["loudness"]
    assert f["status"] == status


def test_an_unrendered_mix_is_unknown():
    f = by_id(run([clip("a", 0, 10)]))["loudness"]
    assert f["status"] == "unknown" and f["evidence"]["target_lufs"] == -14.0


def beat_edit(cut_times, offset=0.0):
    beats = [round(1.0 + i * 0.5, 3) for i in range(60)]                    # 120 BPM in the music's own time
    clips = [clip("bed", 0, 30, kind="audio", role="music", layer=2, file="M")]
    t = 0.0
    for i, c in enumerate(cut_times + [30.0]):
        clips.append(clip(f"p{i}", t, c, layer=1, file="P", src_in=i * 100.0))
        t = c
    return clips, provider(M=audio_index(-20.0, 120.0, beats), P=FakeIndex())


def test_cuts_on_the_beat_pass_and_cuts_off_the_beat_are_flagged_for_music_driven_forms():
    on = [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5]
    clips, idx = beat_edit(on)
    f = by_id(run(clips, Brief(form="montage"), index=idx))["beat_sync"]
    assert f["status"] == "ok" and f["evidence"]["fraction"] == 1.0
    off = [1.75, 2.8, 3.55, 4.7, 5.8, 6.7, 7.9]
    clips, idx = beat_edit(off)
    f = by_id(run(clips, Brief(form="montage"), index=idx))["beat_sync"]
    assert f["status"] == "needs" and "sync_cuts_to_beats_tool" in f["fix"]
    assert by_id(run(clips, Brief(form="documentary"), index=idx))["beat_sync"]["status"] == "info"


def test_beats_follow_the_music_clips_trim_and_start():
    beats = [round(20.25 + 0.5 * i, 3) for i in range(60)]                       # beats in the music's own time
    bed = TimelineClip(id="bed", file_id="M", sha="M", layer=2, kind="audio", start=10.3, end=40.3, src_in=20.0, src_out=50.0, role="music",
                       has_audio=True, gain_db=0.0)
    assert bed.to_timeline(21.0) == pytest.approx(11.3) and bed.to_source(11.3) == pytest.approx(21.0)
    cuts = [bed.to_timeline(beats[i + 2]) for i in range(0, 16, 2)]              # on the beat only if the trim and start are honoured
    clips = [bed] + [clip(f"p{i}", c, c + 1.0, file="P", src_in=i * 100.0) for i, c in enumerate(cuts)]
    f = by_id(run(clips, Brief(form="montage"), index=provider(M=audio_index(-20.0, 120.0, beats), P=FakeIndex())))["beat_sync"]
    assert f["evidence"]["fraction"] == 1.0 and f["status"] == "ok"
    shifted = [c + 0.25 for c in cuts]                                           # a quarter second late: not on the beat
    late = [bed] + [clip(f"p{i}", c, c + 1.0, file="P", src_in=i * 100.0) for i, c in enumerate(shifted)]
    assert by_id(run(late, Brief(form="montage"), index=provider(M=audio_index(-20.0, 120.0, beats), P=FakeIndex())))["beat_sync"]["evidence"]["fraction"] == 0.0


def test_too_few_cuts_or_no_tempo_means_no_beat_verdict():
    clips, idx = beat_edit([2.0, 4.0])
    assert "beat_sync" not in by_id(run(clips, Brief(form="montage"), index=idx))
    clips, _ = beat_edit([1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5])
    assert "beat_sync" not in by_id(run(clips, Brief(form="montage"), index=provider(M=audio_index(-20.0), P=FakeIndex())))


# ============================ text and delivery ============================
def test_captions_must_cover_the_speech_for_social_forms():
    clips = [clip("pic", 0, 20), clip("talk", 2, 12, role="speech")]
    assert by_id(run(clips, Brief(form="TikTok")))["captions"]["status"] == "needs"
    assert by_id(run(clips, Brief(form="TikTok"), captions=[(2.0, 12.0)]))["captions"]["status"] == "ok"
    half = by_id(run(clips, Brief(form="TikTok"), captions=[(2.0, 6.0)]))["captions"]
    assert half["status"] == "needs" and half["evidence"]["coverage"] == 0.4
    assert by_id(run(clips, Brief(form="vlog")))["captions"]["status"] == "ok", "a vlog does not default to burned-in captions"
    assert by_id(run(clips))["captions"]["status"] == "info"
    assert by_id(run(clips, Brief(form="vlog", wants_captions=True)))["captions"]["status"] == "needs"
    assert "captions" not in by_id(run([clip("pic", 0, 20)], Brief(form="TikTok"))), "no speech: nothing to caption"


def test_an_opening_title_is_noticed():
    with_title = [clip("pic", 0, 10), clip("t", 0.5, 3.0, kind="title", layer=4)]
    assert by_id(run(with_title))["opening_title"]["status"] == "ok"
    assert by_id(run([clip("pic", 0, 10)]))["opening_title"]["status"] == "info"


def test_a_vertical_form_needs_a_vertical_project():
    clips = [clip("a", 0, 10)]
    assert by_id(run(clips, Brief(form="Instagram Reel"), project=ProjectInfo(10.0, 1920, 1080)))["format"]["status"] == "needs"
    assert by_id(run(clips, Brief(form="Instagram Reel"), project=ProjectInfo(10.0, 1080, 1920)))["format"]["status"] == "ok"
    assert "format" not in by_id(run(clips, Brief(form="vlog"), project=ProjectInfo(10.0, 1920, 1080)))


# ============================ the whole audit ============================
def test_the_audit_groups_findings_by_layer_and_lists_what_needs_work():
    clips, idx = mixed()
    out = run(clips + [clip("title", 0.5, 3, kind="title", layer=5)], Brief(form="vlog", vibe="warm"), index=idx,
              looks={"pic": look(0.5, 0.0)}, mix={"integrated_lufs": -20.0, "true_peak_db": -3.0})
    assert set(out["layers"]) >= {"story", "picture", "sound", "text"}
    assert "dialogue_margin" in out["needs"] and "loudness" in out["needs"] and "style" in out["needs"]
    assert out["counts"]["needs"] == len(out["needs"]) and sum(out["counts"].values()) == sum(len(v) for v in out["layers"].values())
    assert "consistency" in out["unknown"], "one measured clip cannot show consistency"
    assert all(f["status"] in ("ok", "needs", "info", "unknown") and "fix" in f for v in out["layers"].values() for f in v)


def test_a_clean_edit_has_nothing_that_needs_work():
    clips = [clip(f"p{i}", i * 4.0, i * 4.0 + 4.0, src_in=i * 50.0) for i in range(7)]
    clips += [clip("talk", 0, 8, role="speech", file="S", layer=2), clip("bed", 0, 28, kind="audio", role="music", gain=-16.0, file="M", layer=3),
              clip("ambient", 8, 28, kind="audio", role="ambient", layer=4)]
    idx = provider(S=audio_index(-20.0), M=audio_index(-22.0), F1=FakeIndex([shot(a, a + 50, hook=0.9, highlight=0.9) for a in range(0, 400, 50)]))
    looks = {f"p{i}": look(0.5 + i * 0.005, 0.03) for i in range(7)}
    out = run(clips + [clip("t", 0.5, 3, kind="title", layer=5)], Brief(form="vlog", target_seconds=28), index=idx, looks=looks,
              mix={"integrated_lufs": -14.5, "true_peak_db": -3.0})
    assert out["needs"] == [], [f for v in out["layers"].values() for f in v if f["status"] == "needs"]


@pytest.mark.parametrize("form,expected", [
    ("TikTok", True), ("Instagram Reel", True), ("YouTube Short", True), ("YouTube Shorts", True), ("short-form video", True), ("a short", True),
    ("short film", False), ("Short Film", False), ("short-film", False), ("short documentary", False), ("shorts story", False),
    ("vlog", False), ("shortcut tutorial", False), ("", False)])
def test_a_short_film_is_not_a_short_form_video(form, expected):
    assert R.is_short_form(form) is expected


def test_a_short_film_is_not_held_to_social_rules():
    clips = [clip("pic", 0, 200), clip("talk", 2, 90, kind="audio", role="speech")]
    ids = by_id(run(clips, Brief(form="short film"), project=ProjectInfo(200.0, 1920, 1080)))
    assert "format" not in ids and "form_length" not in ids and ids["captions"]["status"] == "ok"
    reel = by_id(run(clips, Brief(form="Instagram Reel"), project=ProjectInfo(200.0, 1920, 1080)))
    assert reel["format"]["status"] == "needs" and reel["form_length"]["status"] == "needs" and reel["captions"]["status"] == "needs"
