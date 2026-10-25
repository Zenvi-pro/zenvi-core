"""build_reel_tool: a finished short-form reel in one call (mirrors zenvi-web reels.test.ts and plan.test.ts).

The tool runs on the headless editor harness: the real project store, update
manager and undo; libopenshot, the timeline view and Project Files are the
fakes the other editor-tool tests use. The music's beats come from the real
tracker run on a synthetic groove's band energies (tests/beat_fakes.py), so
neither audio nor ffmpeg is needed. The planner and the title cards are pure
and tested directly.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

import beat_fakes as bf
from classes.editor_tools import timeline_edit_reels as reels
from classes.editor_tools.timeline_edit_reels_cards import card_svg, fit_text, flash_svg
from classes.editor_tools.timeline_edit_reels_plan import (
    MIN_SHOT_SECONDS,
    REEL_STYLE_IDS,
    REEL_STYLES,
    MusicBeats,
    ReelMedia,
    choose_music_start,
    cut_beats,
    plan_reel,
)
from classes.speech import beats as speech_beats
from timeline_edit_fakes import fake_openshot
from titles_text_fakes import FakeFilesModel, install_openshot_fakes

FPS = 30
# Layer numbers of UI tracks 1-5 (bottom to top) in a new project.
L1, L2, L3, L4, L5 = 1000000, 2000000, 3000000, 4000000, 5000000



def _curve(*points):
    return {"Points": [{"co": {"X": float(x), "Y": float(y)}, "interpolation": 1} for x, y in points]}


class ReelTimeline:
    """win.timeline: Clip > Motion's handler as the reel uses it, recording calls.

    Like the real Animate_Triggered it recentres the clip, camera moves switch it
    to scale mode crop and zoom over the whole clip, and the title entrances
    animate the first second.
    """

    def __init__(self, editor, fail_on=None):
        self.editor = editor
        self.calls = []
        self.extended = 0
        self.fail_on = fail_on

    def Animate_Triggered(self, action, clip_ids, transaction_id=None, zone_seconds=None, emphasis_seconds=None):
        from classes.query import Clip
        self.calls.append((action.name, list(clip_ids), transaction_id))
        if action.name == self.fail_on:
            raise RuntimeError(f"{action.name} blew up")
        for cid in clip_ids:
            data = Clip.get(id=cid).data
            s = round(float(data["start"]) * FPS) + 1
            e = round(float(data["end"]) * FPS) + 1
            values = {"gravity": 4}
            if action.name.startswith(("CAM_", "KEN_BURNS")):
                values.update(scale=0, scale_x=_curve((s, 1.0), (e, 1.2)), scale_y=_curve((s, 1.0), (e, 1.2)))
            elif action.name == "POP_IN":
                values.update(alpha=_curve((s, 0.0), (s + FPS, 1.0)), scale_x=_curve((s, 0.0), (s + FPS, 1.0)))
            else:  # WIPE_IN_FADE / SLIDE_IN_LEFT
                values.update(location_x=_curve((s, -1.0), (s + FPS, 0.0)))
            self.editor.app.updates.update(["clips", {"id": cid}], values)

    def _extend_timeline_to_fit_items(self):
        self.extended += 1


@pytest.fixture(scope="module")
def groove():
    """60 s of a 120 BPM groove, quiet for the first 8 s, through the real tracker (reels.test.ts's groove())."""
    return bf.analyse(bf.groove_energies(120, 60, quiet_until=8))


@pytest.fixture
def reel(tmp_path, monkeypatch, groove):
    """A vertical 1080x1920 30 fps project with the reel tool's fakes; files live under tmp_path."""
    import classes
    from classes import clip_utils, info, timeline_ops
    from editor_tools_harness import make_editor
    from windows.views import retime

    editor = make_editor(fps=(FPS, 1), width=1080, height=1920)
    # A test elsewhere leaves a fake classes.query in sys.modules; the package attribute is the real one.
    monkeypatch.setitem(sys.modules, "classes.query", classes.query)
    monkeypatch.setattr(info, "TITLE_PATH", str(tmp_path / "title"), raising=False)
    install_openshot_fakes(editor, monkeypatch)
    fake = fake_openshot()
    monkeypatch.setattr(timeline_ops, "openshot", fake)
    monkeypatch.setattr(retime, "openshot", fake)
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.timeline = ReelTimeline(editor)
    editor.window.timeline = editor.timeline
    editor.window.files_model = FakeFilesModel(editor)
    editor.title_dir = tmp_path / "title"
    editor.media = tmp_path / "media"
    editor.media.mkdir()
    editor.analysed = []

    def detect(path, **_kw):
        editor.analysed.append(path)
        return groove

    monkeypatch.setattr(speech_beats, "detect_beats", detect)
    try:
        yield editor
    finally:
        editor.stop()


def _media_file(ed, kind, name, **overrides):
    path = ed.media / name
    path.write_bytes(b"")
    return ed.add_file(kind, path=str(path), **overrides)


def _project(ed):
    """Four landscape clips, a photo and a song in Project Files; nothing on the timeline."""
    ids = {name: _media_file(ed, "video", f"{name}.mp4", duration=20) for name in ("huddle", "dribble", "break", "strike")}
    ids["crowd"] = _media_file(ed, "image", "crowd.png")
    ids["song"] = _media_file(ed, "audio", "song.mp3", duration=60)
    return ids


def _build(ed, **args):
    receipt = ed.call_receipt("build_reel_tool", **args)
    assert receipt["status"] == "applied", receipt["summary"]
    return receipt["data"], receipt


def _refused(ed, *fragments, **args):
    ed.mark()
    receipt = ed.call_receipt("build_reel_tool", **args)
    assert receipt["status"] in ("error", "refused"), receipt["summary"]
    for fragment in fragments:
        assert fragment in receipt["summary"], receipt["summary"]
    assert ed.undo_steps_since_mark() == 0
    return receipt["summary"]


def _on(ed, layer):
    return sorted((c for c in ed.clips() if int(c["layer"]) == layer), key=lambda c: c["position"])


def _length(clip):
    return float(clip["end"]) - float(clip["start"])


# ---------------------------------------------------------------------------
# build_reel_tool (reels.test.ts)
# ---------------------------------------------------------------------------

def test_cuts_a_whole_reel_on_the_musics_beats_in_one_undo_step(reel):
    ids = _project(reel)
    files = [ids[n] for n in ("huddle", "dribble", "break", "strike")]
    reel.mark()
    data, receipt = _build(reel, style="hype", file_ids=files, hook="Match day", end_card="Full game Sunday",
                           duration_seconds="15")
    assert 118 < data["bpm"] < 122
    # The quiet intro is skipped: the music starts in the loud part, on a beat.
    assert data["music_start"] >= 7.9
    assert data["duration"] <= 15
    assert data["shots"][-1]["file_id"] == ids["strike"]
    assert "vertical 9:16" in data["brief"]["format"]
    assert data["brief"]["story"].startswith("huddle") and data["brief"]["story"].endswith("ends on strike")
    assert data["tracks"] == {"music": L1, "video": L2, "flash": L3, "labels": L4, "titles": L5}
    assert data["track_numbers"] == {"music": 1, "video": 2, "flash": 3, "labels": 4, "titles": 5}

    video = _on(reel, data["tracks"]["video"])
    assert len(video) == len(data["shots"])
    assert [c["id"] for c in video] == [s["timeline_clip_id"] for s in data["shots"]]
    # Every cut within a frame of a beat or half-beat of the music (positions on the 30 fps grid).
    grid = [t for i, b in enumerate(data["beats"]) for t in ([b, (b + data["beats"][i + 1]) / 2]
                                                             if i + 1 < len(data["beats"]) else [b])]
    for clip in video:
        assert min(abs(t - clip["position"]) for t in grid) <= 1 / FPS + 1e-6
        assert abs(clip["position"] * FPS - round(clip["position"] * FPS)) < 1e-6
    # Shots butt together with no gap, and the reel ends on the planned last frame.
    for prev, nxt in zip(video, video[1:]):
        assert nxt["position"] == pytest.approx(prev["position"] + _length(prev), abs=1e-9)
    assert video[-1]["position"] + _length(video[-1]) == pytest.approx(data["duration"], abs=1e-3)

    music = _on(reel, data["tracks"]["music"])
    assert len(music) == 1
    assert music[0]["start"] == pytest.approx(data["music_start"], abs=0.01)
    assert music[0]["position"] == 0 and _length(music[0]) == pytest.approx(data["duration"], abs=1e-3)
    volume = music[0]["volume"]["Points"]
    assert volume[-1]["co"]["Y"] == 0 and volume[0]["co"]["Y"] == 0  # faded in from a mid-song start, out at the end
    assert music[0]["has_video"]["Points"][0]["co"]["Y"] == 0  # an audio-only bed paints no picture

    # Hook + end card on the titles track, white flashes on the downbeats (3 s apart at least).
    assert len(_on(reel, data["tracks"]["titles"])) == 2
    flashes = _on(reel, data["tracks"]["flash"])
    assert len(flashes) > 1
    assert all(b["position"] - a["position"] >= 3 - 1e-6 for a, b in zip(flashes, flashes[1:]))
    for flash in flashes:
        assert flash["title"] == "Flash" and _length(flash) == pytest.approx(3 / FPS)
        alpha = flash["alpha"]["Points"]
        assert (alpha[0]["co"]["Y"], alpha[-1]["co"]["Y"]) == (0.75, 0.0)
    written = sorted(os.listdir(reel.title_dir))
    assert "Reel flash 1080x1920.svg" in written
    assert any(n.startswith("Reel hook Match day") for n in written)
    assert any(n.startswith("Reel end Full game Sunday") for n in written)
    assert data["title_clips"]["hook"] and data["title_clips"]["end_card"]

    # Looks on every shot (Color Grade + Film Grain); landscape footage fills the vertical frame.
    for clip in video:
        classes = [e["class_name"] for e in clip["effects"]]
        assert "ColorGrade" in classes and "FilmGrain" in classes
        assert clip["scale"] == 0
    # Every shot pushes in over its whole length (Clip > Motion > Camera's keyframes), centred and
    # cropped, written before the shot is placed: one save per shot.
    for clip in video:
        first, last = round(clip["start"] * FPS) + 1, round(clip["end"] * FPS) + 1
        zoom = {p["co"]["X"]: p["co"]["Y"] for p in clip["scale_x"]["Points"]}
        assert zoom[first] == 1.0 and zoom[last] == pytest.approx(1.2) and clip["gravity"] == 4
    assert not [c for c in reel.timeline.calls if c[0] == "CAM_PUSH_IN"]
    # One batch edit: the minimap and preview catch up at the end, as after Slice or undo.
    batches = [c.args for c in reel.window.IgnoreUpdates.emit.call_args_list]
    assert batches[0] == (True, True) and batches[-1] == (False, True)
    # The hook pops in (style), then fades out over its last quarter second.
    hook = reel.clip(data["title_clips"]["hook"])
    assert ("POP_IN", [hook["id"]]) in [(c[0], c[1]) for c in reel.timeline.calls]
    assert hook["alpha"]["Points"][0]["co"]["Y"] == 0.0 and hook["alpha"]["Points"][-1]["co"]["Y"] == 0.0

    assert reel.undo_steps_since_mark() == 1
    assert receipt["undoSteps"] == 1
    assert reel.timeline.extended >= 1
    reel.undo()
    assert reel.clips() == []


def test_refuses_to_build_over_an_edit_unless_asked_to_replace_it_then_replaces_it_in_one_step(reel):
    _project(reel)
    _build(reel, style="cinematic")
    before = reel.clips()
    _refused(reel, "replace=true", style="minimal")
    reel.mark()
    data, _receipt = _build(reel, style="minimal", replace="true")
    assert data["style"] == "minimal" and reel.undo_steps_since_mark() == 1
    assert {c["id"] for c in before}.isdisjoint(c["id"] for c in reel.clips())
    reel.undo()
    assert sorted(c["id"] for c in reel.clips()) == sorted(c["id"] for c in before)


def test_cuts_to_the_styles_tempo_and_says_so_when_the_music_cannot_be_analysed(reel, monkeypatch):
    _project(reel)

    def fail(path, **_kw):
        raise RuntimeError("ffmpeg not found on PATH")

    monkeypatch.setattr(speech_beats, "detect_beats", fail)
    data, receipt = _build(reel, style="match_cut", duration_seconds=8)
    assert "Cut to the style's tempo instead" in receipt["summary"]
    assert any("ffmpeg not found" in w for w in data["warnings"])
    # match_cut cuts on every beat of its 120 BPM fallback after a two-beat opening.
    assert data["shots"][1]["start"] == pytest.approx(1.0, abs=1e-3)
    assert data["shots"][2]["start"] == pytest.approx(1.5, abs=1e-3)
    assert data["music_start"] is None and data["bpm"] == 120
    # The music still plays under the reel, from its start.
    assert len(_on(reel, data["tracks"]["music"])) == 1


def test_refuses_unknown_and_non_visual_files_without_touching_history(reel):
    ids = _project(reel)
    assert "NOPE000000" in _refused(reel, "unknown file id", file_ids=["NOPE000000"])
    _refused(reel, "not videos or images", file_ids=[ids["song"]])
    _refused(reel, "unknown music_file_id", music_file_id="NOPE000000")
    _refused(reel, "has no audio", music_file_id=ids["huddle"])
    assert reel.clips() == []


# ---------------------------------------------------------------------------
# build_reel_tool on the desktop
# ---------------------------------------------------------------------------

def test_given_beats_are_used_instead_of_analysing_the_music(reel):
    _project(reel)
    beats = [round(1 + i * 0.6, 3) for i in range(90)]  # 100 BPM
    data, _receipt = _build(reel, style="highlight", beats=json.dumps(beats))
    assert reel.analysed == []
    assert data["bpm"] == pytest.approx(100, abs=0.5)
    assert data["music_start"] == pytest.approx(1.0, abs=1 / FPS)


def test_defaults_use_every_video_and_image_and_the_longest_song(reel):
    ids = _project(reel)
    short = _media_file(reel, "audio", "sting.mp3", duration=5)
    data, _receipt = _build(reel, style="cinematic")
    used = {s["file_id"] for s in data["shots"]}
    assert used <= {ids[n] for n in ("huddle", "dribble", "break", "strike", "crowd")} and ids["crowd"] in used
    assert data["music_file_id"] == ids["song"] != short
    assert reel.analysed == [str(reel.media / "song.mp3")]


def test_refuses_locked_tracks_and_media_missing_on_disk(reel):
    ids = _project(reel)
    reel.lock_track(L3)
    _refused(reel, "track 3 is locked")
    reel.lock_track(L3, False)
    os.unlink(reel.media / "dribble.mp4")
    _refused(reel, "dribble is missing on disk")
    _refused(reel, "file_ids", file_ids=[])
    assert reel.clips() == [] and ids


def test_a_failure_part_way_rolls_the_whole_edit_back(reel):
    _project(reel)
    reel.timeline.fail_on = "POP_IN"
    reel.manager.redoHistory.append(object())  # a redo step from earlier survives the failed call
    history = list(reel.manager.actionHistory)
    redo = list(reel.manager.redoHistory)
    summary = _refused(reel, "blew up", style="hype", hook="Match day")
    assert summary.startswith("Error")
    assert reel.clips() == []
    assert list(reel.manager.actionHistory) == history and list(reel.manager.redoHistory) == redo
    # Its title files are gone again (Project Files and disk).
    assert not [f for f in reel.get("files") if str(f.get("path", "")).endswith(".svg")]
    assert not os.path.isdir(reel.title_dir) or os.listdir(reel.title_dir) == []


def test_every_shot_fills_the_frame(reel):
    vertical = _media_file(reel, "video", "selfie.mp4", duration=12, width=1080, height=1920)
    near = _media_file(reel, "video", "tall.mp4", duration=12, width=1080, height=2048)
    wide = _media_file(reel, "video", "wide.mp4", duration=12)
    unknown = _media_file(reel, "video", "odd.mp4", duration=12, width=0, height=0)
    data, _receipt = _build(reel, style="editorial", file_ids=[vertical, near, wide, unknown])  # no camera moves
    assert reel.timeline.calls == []
    scale = {s["file_id"]: reel.clip(s["timeline_clip_id"])["scale"] for s in data["shots"]}
    # Scale mode crop: landscape footage is cropped, not letterboxed, and a near-match leaves no thin
    # bars (at the frame's own aspect crop is fit). A clip of unknown size keeps the default.
    assert scale[vertical] == scale[near] == scale[wide] == 0
    assert scale[unknown] == 1


def test_labels_titles_and_end_plate_follow_the_style(reel):
    ids = _project(reel)
    files = [ids["huddle"], ids["dribble"], ids["strike"]]
    data, _receipt = _build(reel, style="editorial", file_ids=files, labels=["Warm-up", "", "The goal"],
                            end_card="Sunday 3 PM", end_card_subtitle="Live on Zenvi")
    labels = _on(reel, data["tracks"]["labels"])
    labelled = [s for s in data["shots"] if s["file_id"] in (ids["huddle"], ids["strike"])]
    assert len(labels) == len(labelled)
    assert [c["position"] for c in labels] == [pytest.approx(s["start"], abs=1e-3) for s in labelled]
    names = sorted(os.listdir(reel.title_dir))
    assert any(n.startswith("Reel label Warm-up") for n in names) and any("The goal" in n for n in names)
    end = next(n for n in names if n.startswith("Reel end"))
    svg = (reel.title_dir / end).read_text()
    assert "SUNDAY 3 PM" in svg and "Live on Zenvi" in svg and 'fill-opacity="0.5"' in svg  # the plate
    # editorial slides the end card in from the left.
    assert ("SLIDE_IN_LEFT", [data["title_clips"]["end_card"]]) in [(c[0], c[1]) for c in reel.timeline.calls]


def test_a_short_clip_is_slowed_to_fill_its_shot_without_a_gap(reel):
    tiny = _media_file(reel, "video", "tiny.mp4", duration=0.6)
    short = _media_file(reel, "video", "short.mp4", duration=0.9)
    _media_file(reel, "audio", "song.mp3", duration=60)
    data, _receipt = _build(reel, style="cinematic", file_ids=[tiny, short], duration_seconds=12)
    video = _on(reel, data["tracks"]["video"])
    assert video[0]["position"] == 0
    for prev, nxt in zip(video, video[1:]):
        assert nxt["position"] == pytest.approx(prev["position"] + _length(prev), abs=1e-9)
    slowed = [c for c in video if len(c["time"]["Points"]) > 1]
    assert slowed, "a clip shorter than its shot plays slower (Clip > Speed's retime)"


def test_every_style_uses_the_desktops_own_look_and_motion_presets():
    from classes.editor_tools.effects_color_looks import LOOK_PRESETS
    photo = {"width": 1880, "height": 1253}
    for style in REEL_STYLES.values():
        for look in style.looks:
            assert look.replace(".", "_", 1) in LOOK_PRESETS, (style.id, look)
            assert reels.look_preset(look)
        # Shots move with Clip > Motion > Camera's keyframes; the hook enters with the menu's handler.
        for preset in (style.motion_video, style.motion_image):
            assert preset in (None, "animate.none", *reels.CAMERA_MOVES), (style.id, preset)
            assert (reels.camera_move(preset, (1080, 1920), photo) is None) == (preset in (None, "animate.none"))
        assert reels.motion_action(style.typography.hook_in) is not None, style.id


def test_the_shared_camera_keyframes_replace_the_moved_range_and_keep_the_rest():
    from classes.camera_motion import apply_camera_keyframes, push_pull_keyframes

    def point(frame, value):
        return {"co": {"X": float(frame), "Y": float(value)}, "interpolation": 0}

    data = {prop: {"Points": [point(1, 1.0 if prop.startswith("scale") else 0.0)]}
            for prop in ("scale_x", "scale_y", "location_x", "location_y")}
    data["scale_x"]["Points"] += [point(40, 3.0), point(200, 2.0)]
    apply_camera_keyframes(data, push_pull_keyframes(zoom_in=True), 31, 91, point)
    assert [(p["co"]["X"], p["co"]["Y"]) for p in data["scale_x"]["Points"]] == [
        (1.0, 1.0), (200.0, 2.0), (31.0, 1.0), (91.0, 1.2)]  # 40 was inside the move
    assert [(p["co"]["X"], p["co"]["Y"]) for p in data["location_y"]["Points"]] == [
        (1.0, 0.0), (31.0, 0.0), (91.0, 0.0)]


# ---------------------------------------------------------------------------
# Planning (plan.test.ts)
# ---------------------------------------------------------------------------

def _music() -> MusicBeats:
    """120 BPM from 1 s for 60 s; quiet for the first 8 bars, loud after."""
    beats = [1 + i * 0.5 for i in range(120)]
    return MusicBeats(bpm=120, beats=beats, downbeats=[t for i, t in enumerate(beats) if i % 4 == 0],
                      beat_energy=[0.2 if t < 17 else 0.9 for t in beats], duration=61.5)


def _clip(file_id, seconds=10.0):
    return ReelMedia(file_id=file_id, kind="video", start=0, end=seconds)


def _contiguous(shots, duration):
    assert shots[0].start == 0
    for prev, nxt in zip(shots, shots[1:]):
        assert nxt.start == pytest.approx(prev.end, abs=1e-6)
    assert shots[-1].end == pytest.approx(duration, abs=1e-6)


def test_music_starts_on_a_downbeat_of_the_loud_part():
    assert choose_music_start(_music(), 15) == 17


def test_music_keeps_its_first_downbeat_when_the_track_is_too_short_to_choose():
    music = _music()
    short = MusicBeats(music.bpm, music.beats, music.downbeats, music.beat_energy, duration=16)
    assert choose_music_start(short, 15) == 1


def test_cut_points_hold_the_first_and_last_shots_and_follow_the_pattern_between():
    cuts = cut_beats(REEL_STYLES["cinematic"], 32)
    assert cuts[:3] == [0, 4, 6] and cuts[-1] == 32
    assert 32 - cuts[-2] >= REEL_STYLES["cinematic"].last_beats


def test_plan_cuts_a_hype_reel_on_the_beat_grid_with_faster_cuts_in_the_climax():
    plan = plan_reel(REEL_STYLES["hype"], [_clip(i) for i in ("a", "b", "c", "d", "payoff")], _music(), seconds=15)
    assert plan.music_start == 17 and plan.duration <= 15
    assert any(abs(d - plan.duration) < 1e-3 for d in plan.downbeats)
    _contiguous(plan.shots, plan.duration)
    # Every cut on a beat or half-beat of the 120 BPM grid (0.25 s steps).
    assert all(abs(s.start * 4 - round(s.start * 4)) < 1e-6 for s in plan.shots)
    assert plan.shots[0].end == pytest.approx(1, abs=1e-6)
    body = [s for s in plan.shots if not s.climax][1:-1]
    climax = [s for s in plan.shots if s.climax][:-1]

    def avg(shots):
        return sum(s.end - s.start for s in shots) / len(shots)

    assert avg(climax) < avg(body)
    # The payoff closes the reel; flashes can find the downbeats.
    assert plan.shots[-1].file_id == "payoff"
    assert sum(s.on_downbeat for s in plan.shots) > 2


def test_plan_shows_a_different_moment_each_time_a_clip_comes_back():
    plan = plan_reel(REEL_STYLES["match_cut"], [_clip("only", 30)], _music(), seconds=12)
    starts = [s.source_start for s in plan.shots]
    assert len(set(starts)) == len(starts)
    for shot in plan.shots:
        assert shot.source_end - shot.source_start == pytest.approx(shot.end - shot.start, abs=1e-6)
    # The first use starts a third of the way in (stock footage opens on a rack focus or a shake).
    assert plan.shots[0].source_start == pytest.approx((30 - 1) * 0.3, abs=1e-3)


def test_plan_splits_a_shot_on_the_beat_or_slows_a_clip_instead_of_leaving_a_gap():
    plan = plan_reel(REEL_STYLES["cinematic"], [_clip("tiny", 0.6), _clip("short", 0.9)], _music(), seconds=12)
    _contiguous(plan.shots, plan.duration)
    for shot in plan.shots:
        assert shot.end - shot.start >= MIN_SHOT_SECONDS - 1e-6
        assert (shot.source_end - shot.source_start) / shot.speed >= shot.end - shot.start - 1e-3


def test_plan_cuts_to_the_styles_own_tempo_without_music_and_keeps_stills_whole():
    media = [ReelMedia(file_id="photo", kind="image", start=0, end=0), _clip("v")]
    plan = plan_reel(REEL_STYLES["minimal"], media, None, seconds=20)
    assert plan.music_start is None and plan.bpm == REEL_STYLES["minimal"].default_bpm
    _contiguous(plan.shots, plan.duration)
    assert plan.shots[0].end - plan.shots[0].start == pytest.approx((60 / 80) * 4, abs=1e-3)


def test_plan_puts_every_cut_and_in_point_on_the_frame_grid():
    music = _music()
    fast = MusicBeats(140, [t * 120 / 140 for t in music.beats], [t * 120 / 140 for t in music.downbeats],
                      music.beat_energy, music.duration)
    plan = plan_reel(REEL_STYLES["hype"], [_clip(i) for i in "abc"], fast, seconds=14, frame_rate=FPS)
    for shot in plan.shots:
        assert abs(shot.start * FPS - round(shot.start * FPS)) < 1e-9
        assert abs(shot.end * FPS - round(shot.end * FPS)) < 1e-9
    for prev, nxt in zip(plan.shots, plan.shots[1:]):
        assert nxt.start == prev.end
    # Source windows are whole frames, exactly as long as their shots (a rounded in and out point lost one).
    for shot in (s for s in plan.shots if s.speed == 1):
        assert abs(shot.source_start * FPS - round(shot.source_start * FPS)) < 0.05
        assert round((shot.source_end - shot.source_start) * FPS) == round((shot.end - shot.start) * FPS)


def test_plan_refuses_an_empty_media_list():
    with pytest.raises(ValueError, match="at least one"):
        plan_reel(REEL_STYLES["hype"], [], None)


# ---------------------------------------------------------------------------
# Title cards (typography.ts)
# ---------------------------------------------------------------------------

def test_cards_fit_the_frame_escape_the_text_and_follow_the_style():
    bold = REEL_STYLES["hype"].typography
    one = fit_text("MATCH DAY", bold, 1080 * 0.84, 1920 * 0.26, 146)
    assert one.lines == ["MATCH DAY"] and 120 < one.size <= 146
    long = fit_text("THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG", bold, 907, 499, 146)
    assert 1 < len(long.lines) <= 3 and long.size < 146

    hook = card_svg(1080, 1920, "Tom & <Jerry>", "hook", bold)
    assert hook.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
                           'width="1080" height="1920" viewBox="0 0 1080 1920">')
    assert "TOM &amp;" in hook and "&lt;JERRY&gt;" in hook and 'font-weight="900"' in hook
    assert hook.count("<text ") in (2, 4)  # each line and its shadow
    calm = card_svg(1920, 1080, "slow morning", "hook", REEL_STYLES["minimal"].typography)
    assert ">slow morning<" in calm  # minimal keeps the case
    label = card_svg(1080, 1920, "Day 1", "label", REEL_STYLES["editorial"].typography)
    assert 'text-anchor="start"' in label and ">DAY 1<" in label and "<rect " in label
    assert flash_svg(1080, 1920).count('fill="#ffffff"') == 1
    for style_id in REEL_STYLE_IDS:
        end = card_svg(1080, 1350, "Full game Sunday", "end", REEL_STYLES[style_id].typography, "Subscribe")
        assert ("fill-opacity=\"0.5\"" in end) == REEL_STYLES[style_id].typography.end_plate
        assert ">Subscribe<" in end and end.count("<text ") % 2 == 0  # every line has its shadow
