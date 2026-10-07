"""The pre-existing AI video tools (modify_clip / generate_transition_clip / generate_video): originals
are really replaced, in ONE undo step, and every failure starts with Error.

The generation itself (ffmpeg, Runware, downloads) is mocked; the timeline edits run on the
real project store and undo machinery.
"""

import json

import pytest

from ai_generation_fakes import FakePoint, FakeTimeline
from classes import tool_handlers as th

L1, L2 = 1000000, 2000000
THREAD = object()


class _SameThread:
    """QThread stand-in: every call is already on the 'GUI' thread (runs inline)."""

    @staticmethod
    def currentThread():
        return THREAD


@pytest.fixture
def qt_inline(editor, monkeypatch):
    editor.app.thread.return_value = THREAD
    monkeypatch.setattr(th, "QThread", _SameThread)
    monkeypatch.setattr(th, "QEventLoop", object)
    monkeypatch.setattr(th, "QPointF", FakePoint)
    editor.window.timeline = FakeTimeline(editor)
    return editor


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


def _span(clip):
    return clip["position"], clip["position"] + clip["end"] - clip["start"]


def _tr(editor, layer, position, length):
    """A transition (Mask) on *layer*."""
    from classes.query import Transition
    t = Transition()
    t.data = {"layer": layer, "position": position, "start": 0.0, "end": length, "type": "Mask", "title": "Fade"}
    t.save()
    editor.mark()
    return t.id


def _transition(editor, tid):
    return next((t for t in editor.get("effects") or [] if t.get("id") == tid), None)


# --- the three install helpers --------------------------------------------------------------------

def test_insert_replaces_the_clip_and_ripples_later_items_on_its_track(editor, qt_inline):
    video = editor.add_file("video", duration=30)
    baked = editor.add_file("video", path="/media/baked.mp4", duration=13.0)
    c = editor.add_clip(video, position=2.0, layer=L1, start=1.0, end=11.0)
    later = editor.add_clip(video, position=12.0, layer=L1, end=2.0)
    fade_out = _tr(editor, L1, 11.0, 1.0)        # after the insert point: follows the clip's tail
    fade_in = _tr(editor, L1, 2.0, 1.0)          # before it: stays
    other = editor.add_clip(video, position=12.0, layer=L2, end=2.0)
    with th._transaction(editor.app):
        r = th._install_baked_insert(c, baked, 8.0)     # insert 8 s into the clip (timeline 10 s)
    assert editor.clip(c) is None
    new = editor.clip(r["timeline_clip_id"])
    assert new["layer"] == L1 and _span(new) == (2.0, pytest.approx(15.0))
    assert editor.clip(later)["position"] == pytest.approx(15.0)       # moved by the 3 s growth
    assert _transition(editor, fade_out)["position"] == pytest.approx(14.0)
    assert _transition(editor, fade_in)["position"] == 2.0
    assert editor.clip(other)["position"] == 12.0
    assert set(r["moved_later_items"]) == {later, fade_out} and r["moved_by"] == pytest.approx(3.0)
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(c) is not None and editor.clip(r["timeline_clip_id"]) is None
    assert editor.clip(later)["position"] == 12.0 and _transition(editor, fade_out)["position"] == 11.0


def test_replace_swaps_the_head_and_keeps_the_rest_of_the_clip(editor, qt_inline):
    video = editor.add_file("video", duration=30)
    edit = editor.add_file("video", path="/media/edit.mp4", duration=5.04)
    c = editor.add_clip(video, position=2.0, layer=L1, start=1.0, end=11.0)
    with th._transaction(editor.app):
        r = th._install_baked_head(c, edit, 5.0, 5.04)
    kept = editor.clip(c)
    assert kept["start"] == pytest.approx(6.0) and kept["position"] == pytest.approx(7.0)
    new = editor.clip(r["timeline_clip_id"])
    assert _span(new) == (2.0, pytest.approx(7.0))
    assert r["rest_of_original"] == c and r["replaced"] == []
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(c)["start"] == 1.0 and editor.clip(c)["position"] == 2.0
    assert editor.clip(r["timeline_clip_id"]) is None


def test_replace_of_the_whole_clip_removes_it(editor, qt_inline):
    video = editor.add_file("video", duration=30)
    edit = editor.add_file("video", path="/media/edit.mp4", duration=4.0)
    c = editor.add_clip(video, position=0.0, layer=L1, start=0.0, end=4.0)
    with th._transaction(editor.app):
        r = th._install_baked_head(c, edit, 4.0, 4.0)
    assert editor.clip(c) is None and r["replaced"] == [c]
    assert _span(editor.clip(r["timeline_clip_id"])) == (0.0, pytest.approx(4.0))


def test_morph_replaces_both_clips_and_their_cut_and_makes_room(editor, qt_inline):
    video = editor.add_file("video", duration=30)
    baked = editor.add_file("video", path="/media/morph.mp4", duration=12.0)
    a = editor.add_clip(video, position=0.0, layer=L1, end=4.0)
    b = editor.add_clip(video, position=4.5, layer=L1, end=3.0)          # 0.5 s gap
    cut = _tr(editor, L1, 3.8, 1.0)                                         # across the A|B cut
    b_fade = _tr(editor, L1, 7.0, 0.5)                                      # fade out at B's end
    later = editor.add_clip(video, position=8.0, layer=L1, end=2.0)
    with th._transaction(editor.app):
        r = th._install_baked_morph(a, b, baked)
    assert editor.clip(a) is None and editor.clip(b) is None and _transition(editor, cut) is None
    assert _span(editor.clip(r["timeline_clip_id"])) == (0.0, pytest.approx(12.0))
    assert r["moved_by"] == pytest.approx(4.5)                             # 5 s morph - 0.5 s gap
    assert editor.clip(later)["position"] == pytest.approx(12.5)
    assert _transition(editor, b_fade)["position"] == pytest.approx(11.5)
    assert r["removed_transitions"] == [cut]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(a) and editor.clip(b) and _transition(editor, cut)
    assert editor.clip(later)["position"] == 8.0


def test_morph_across_a_wide_gap_moves_only_bs_own_items(editor, qt_inline):
    video = editor.add_file("video", duration=30)
    baked = editor.add_file("video", path="/media/morph.mp4", duration=12.0)
    a = editor.add_clip(video, position=0.0, layer=L1, end=4.0)
    b = editor.add_clip(video, position=11.0, layer=L1, end=3.0)         # 7 s gap > the 5 s morph
    b_fade = _tr(editor, L1, 13.5, 0.5)
    later = editor.add_clip(video, position=15.0, layer=L1, end=2.0)
    with th._transaction(editor.app):
        th._install_baked_morph(a, b, baked)
    assert _transition(editor, b_fade)["position"] == pytest.approx(11.5)
    assert editor.clip(later)["position"] == 15.0                           # never pulled left


def test_a_clip_removed_during_generation_is_an_error(editor, qt_inline):
    baked = editor.add_file("video", path="/media/baked.mp4", duration=13.0)
    with pytest.raises(RuntimeError, match="no longer on the timeline"):
        th._install_baked_insert("gone", baked, 1.0)


# --- validation before any credits ------------------------------------------------------------------

def test_morph_pair_checks(editor, qt_inline):
    from classes.query import Clip
    video = editor.add_file("video", duration=30)
    a = editor.add_clip(video, position=0.0, layer=L1, end=4.0)
    b = editor.add_clip(video, position=5.0, layer=L1, end=3.0)
    c = editor.add_clip(video, position=5.0, layer=L2, end=3.0)
    ca, cb, cc = Clip.get(id=a), Clip.get(id=b), Clip.get(id=c)
    err, first, second = th._check_morph_pair(editor.app, cb, ca)
    assert err == "" and (first.id, second.id) == (a, b), "ordered by timeline position"
    assert "different tracks" in th._check_morph_pair(editor.app, ca, cc)[0]
    assert "same clip" in th._check_morph_pair(editor.app, ca, ca)[0]
    mid = editor.add_clip(video, position=4.2, layer=L1, end=0.5)
    assert mid in th._check_morph_pair(editor.app, ca, cb)[0]
    editor.lock_track(L1)
    assert "is locked" in th._check_morph_pair(editor.app, ca, cb)[0]


def test_transition_refuses_clips_on_different_tracks_before_spending(editor, qt_inline, monkeypatch):
    from classes import credits_client
    monkeypatch.setattr(credits_client, "check_operation", lambda *a, **k: pytest.fail("credits checked"))
    video = editor.add_file("video", duration=30)
    a = editor.add_clip(video, position=0.0, layer=L1, end=4.0)
    b = editor.add_clip(video, position=4.0, layer=L2, end=3.0)
    out = editor.call("generate_transition_clip_tool", clip_a_id=a, clip_b_id=b)
    assert out.startswith("Error: the two clips are on different tracks")
    assert editor.undo_steps_since_mark() == 0


@pytest.mark.parametrize("mode", ["insert", "replace"])
def test_modify_clip_on_a_locked_track_is_refused_before_any_work(editor, qt_inline, monkeypatch, mode):
    monkeypatch.setattr(th, "_ffmpeg_run", lambda *a, **k: pytest.fail("ffmpeg ran"))
    video = editor.add_file("video", duration=30)
    c = editor.add_clip(video, position=0.0, layer=L1, end=10.0)
    editor.lock_track(L1)
    out = editor.call("modify_clip_tool", mode=mode, description="a red balloon floats by", timeline_clip_id=c)
    assert out.startswith("Error: Track") and "is locked" in out


def test_modify_clip_rejects_an_unknown_mode(editor, qt_inline):
    assert th.modify_clip(mode="restyle", description="x").startswith("Error: mode must be 'replace' or 'insert'")


# --- full flows with the generation mocked -----------------------------------------------------------

@pytest.fixture
def generation(editor, qt_inline, monkeypatch, tmp_path):
    """Mock ffmpeg / Runware / downloads / import; imports insert a real File (same undo step)."""
    from unittest.mock import MagicMock

    from classes import api_client, assets, credits_client
    from classes.query import File

    client = MagicMock()
    client.generate_video.return_value = {"video_url": "https://example.invalid/v.mp4"}
    client.upload_media_file.return_value = {"success": True}
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)
    monkeypatch.setattr(credits_client, "check_operation", lambda *a, **k: (True, 100, None))
    charges = []
    monkeypatch.setattr(credits_client, "charge_operation_on_success", lambda *a, **k: charges.append((a, k)))
    monkeypatch.setattr(credits_client.credits, "award_bonus", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(assets, "durable_media_path", lambda ext=".mp4", project_file_path=None:
                        str(tmp_path / ("out" + ext)))

    def ffmpeg(args):
        out = args[-1]
        if isinstance(out, str) and out.startswith(str(tmp_path)) or "frames:v" in " ".join(map(str, args)):
            try:
                with open(out, "wb") as fh:
                    fh.write(b"x" * 2048)
            except OSError:
                pass
        return True, ""

    monkeypatch.setattr(th, "_ffmpeg_run", ffmpeg)
    monkeypatch.setattr(th, "_download_video_url_to_path", lambda url, dest, timeout=180: open(dest, "wb").write(
        b"v") and None)
    durations = {"value": 3.4}
    monkeypatch.setattr(th, "_ffprobe_video_duration", lambda path: durations["value"])
    monkeypatch.setattr(th, "_ffprobe_has_audio", lambda path: False)
    monkeypatch.setattr(th, "_bake_transition_video", lambda *a, **k: (True, ""))
    imported = {"duration": 13.0}

    def fake_import(path, preserve_alpha=None):
        f = File()
        f.data = {"path": "/media/generated_%d.mp4" % len(editor.get("files") or []), "media_type": "video",
                  "duration": imported["duration"], "has_video": True, "has_audio": False, "width": 1920,
                  "height": 1080, "fps": {"num": 30, "den": 1},
                  "video_length": str(int(imported["duration"] * 30))}
        f.save()
        return f, None

    monkeypatch.setattr(th, "_import_generated_video", fake_import)
    return type("G", (), {"client": client, "charges": charges, "durations": durations, "imported": imported})


def test_modify_clip_insert_end_to_end_is_one_undo_step(editor, generation, tmp_path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"src")
    video = editor.add_file("video", path=str(src), duration=30)
    c = editor.add_clip(video, position=2.0, layer=L1, start=0.0, end=10.0)
    later = editor.add_clip(video, position=12.0, layer=L1, end=2.0)
    generation.imported["duration"] = 13.0
    out = editor.call("modify_clip_tool", mode="insert", description="a red balloon floats by",
                      timeline_clip_id=c)
    r = _receipt(out)
    assert out.startswith(f"Replaced clip {c} with the combined clip")
    assert editor.clip(c) is None and editor.clip(r["timeline_clip_id"])["position"] == 2.0
    assert editor.clip(later)["position"] == pytest.approx(15.0)
    assert editor.undo_steps_since_mark() == 1, "import + delete + place + ripple = one step"
    editor.undo()
    assert editor.clip(c) is not None and editor.clip(later)["position"] == 12.0
    assert len(editor.get("files")) == 1, "the generated file goes with the same undo"


def test_modify_clip_replace_end_to_end(editor, generation, tmp_path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"src")
    video = editor.add_file("video", path=str(src), duration=30)
    c = editor.add_clip(video, position=0.0, layer=L1, start=0.0, end=12.0)
    generation.durations["value"] = 5.0
    generation.imported["duration"] = 5.0
    out = editor.call("modify_clip_tool", mode="replace", description="make the car red", timeline_clip_id=c)
    r = _receipt(out)
    assert out.startswith(f"Replaced the first 5.0s of clip {c}")
    assert editor.clip(c)["start"] == pytest.approx(5.0) and editor.clip(c)["position"] == pytest.approx(5.0)
    assert _span(editor.clip(r["timeline_clip_id"])) == (0.0, pytest.approx(5.0))
    assert editor.undo_steps_since_mark() == 1


def test_generate_transition_end_to_end(editor, generation, tmp_path):
    pa, pb = tmp_path / "a.mp4", tmp_path / "b.mp4"
    pa.write_bytes(b"a")
    pb.write_bytes(b"b")
    fa = editor.add_file("video", path=str(pa), duration=20)
    fb = editor.add_file("video", path=str(pb), duration=20)
    a = editor.add_clip(fa, position=0.0, layer=L1, end=4.0)
    b = editor.add_clip(fb, position=4.0, layer=L1, end=3.0)
    later = editor.add_clip(fa, position=7.0, layer=L1, end=2.0)
    generation.durations["value"] = 12.0
    generation.imported["duration"] = 12.0
    out = editor.call("generate_transition_clip_tool", clip_a_id=b, clip_b_id=a)   # order does not matter
    r = _receipt(out)
    assert editor.clip(a) is None and editor.clip(b) is None
    assert editor.clip(later)["position"] == pytest.approx(12.0)
    assert r["replaced"] == [a, b]
    assert editor.undo_steps_since_mark() == 1


@pytest.mark.parametrize("asked, sent", [("", 5), ("7", 7), ("12.4", 12), ("1", 2), ("40", 15), ("soon", 5),
                                         ("inf", 5)])
def test_generate_video_sends_any_duration_from_2_to_15_seconds(editor, generation, monkeypatch, asked, sent):
    placed = {}
    monkeypatch.setattr(th, "add_clip_to_timeline", lambda **kw: placed.update(kw) or "Error: not placing here")
    editor.call("generate_video_and_add_to_timeline_tool", prompt="waves", duration_seconds=asked)
    assert generation.client.generate_video.call_args.kwargs["duration_seconds"] == sent
    # Placement keeps exactly what was generated (never trims a paid second away).
    assert placed["duration_seconds"] == (str(sent) if asked else "")


@pytest.mark.parametrize("asked, sent", [(None, 5), ("3", 3), ("60", 15)])
def test_generate_transition_morph_length_is_variable(editor, generation, tmp_path, asked, sent):
    pa, pb = tmp_path / "a.mp4", tmp_path / "b.mp4"
    pa.write_bytes(b"a")
    pb.write_bytes(b"b")
    a = editor.add_clip(editor.add_file("video", path=str(pa), duration=20), position=0.0, layer=L1, end=4.0)
    b = editor.add_clip(editor.add_file("video", path=str(pb), duration=20), position=4.0, layer=L1, end=3.0)
    args = {} if asked is None else {"duration_seconds": asked}
    editor.call("generate_transition_clip_tool", clip_a_id=a, clip_b_id=b, **args)
    call = generation.client.generate_video.call_args.kwargs
    assert call["mode"] == "frame_morph" and call["duration_seconds"] == sent


def test_modify_clip_replace_never_sends_more_than_the_8s_edit_limit(editor, generation, monkeypatch, tmp_path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"src")
    c = editor.add_clip(editor.add_file("video", path=str(src), duration=30), position=0.0, layer=L1, end=20.0)
    extracted = []
    real = th._ffmpeg_run
    monkeypatch.setattr(th, "_ffmpeg_run", lambda args: (extracted.append(args), real(args))[1])
    editor.call("modify_clip_tool", mode="replace", description="make the car red", timeline_clip_id=c,
                duration_seconds="15")
    assert extracted[0][extracted[0].index("-t") + 1] == "8.0"


def test_generate_video_ripple_and_placement_are_one_step(editor, generation, monkeypatch):
    video = editor.add_file("video", duration=30)
    first = editor.add_clip(video, position=0.0, layer=L1, end=4.0)
    later = editor.add_clip(video, position=4.0, layer=L1, end=4.0)
    generation.imported["duration"] = 5.0

    def fake_add(**kw):
        new = editor.window.timeline.addClip(kw["file_id"], FakePoint(float(kw["position_seconds"])), L1,
                                             call_manual_move=False)
        return f"Added clip to timeline at position {kw['position_seconds']}s timeline_clip_id={new['id']}."

    monkeypatch.setattr(th, "add_clip_to_timeline", fake_add)
    out = editor.call("generate_video_and_add_to_timeline_tool", prompt="waves at sunset", position_seconds="4",
                      track="1")
    assert out.startswith("Added clip"), out
    assert editor.clip(later)["position"] == pytest.approx(9.0) and editor.clip(first)["position"] == 0.0
    assert editor.undo_steps_since_mark() == 1, "import, ripple and placement undo together"
    editor.undo()
    assert editor.clip(later)["position"] == 4.0 and editor.get("files")[-1]["id"] == video


def test_generate_video_failed_placement_puts_the_ripple_back(editor, generation, monkeypatch):
    video = editor.add_file("video", duration=30)
    later = editor.add_clip(video, position=4.0, layer=L1, end=4.0)
    generation.imported["duration"] = 5.0
    monkeypatch.setattr(th, "add_clip_to_timeline", lambda **kw: "Error: something broke")
    out = editor.call("generate_video_and_add_to_timeline_tool", prompt="waves", position_seconds=4, track="1")
    assert out.startswith("Error: Video imported (file_id=") and "were put back" in out
    assert editor.clip(later)["position"] == 4.0


@pytest.mark.parametrize("args, message", [
    ({"track": "9"}, "Error"),
    ({"position_seconds": "soon"}, "is not a time"),
])
def test_generate_video_checks_track_and_time_before_generating(editor, generation, args, message):
    out = editor.call("generate_video_and_add_to_timeline_tool", prompt="waves", **args)
    assert out.startswith("Error") and message in out
    generation.client.generate_video.assert_not_called()


def test_generate_video_on_a_locked_track_is_refused_before_generating(editor, generation):
    video = editor.add_file("video", duration=30)
    editor.add_clip(video, position=4.0, layer=L1, end=4.0)
    editor.lock_track(L1)
    out = editor.call("generate_video_and_add_to_timeline_tool", prompt="waves", position_seconds=4)
    assert out.startswith("Error: Track") and "locked" in out
    generation.client.generate_video.assert_not_called()


def test_credit_blocks_start_with_error(editor, generation, monkeypatch):
    from classes import credits_client
    monkeypatch.setattr(credits_client, "check_operation",
                        lambda *a, **k: (False, 0, "Insufficient credits (0 remaining, need 25 for video)."))
    out = editor.call("generate_video_and_add_to_timeline_tool", prompt="waves")
    assert out == "Error: Insufficient credits (0 remaining, need 25 for video)."


def test_generated_video_keeps_its_own_aspect():
    assert th._fit_generated_size(1080, 1920) == (1080, 1920)
    assert th._fit_generated_size(3840, 2160) == (1920, 1080)
    assert th._fit_generated_size(1441, 1441) == (1440, 1440)
    assert th._fit_generated_size(2160, 2160) == (1920, 1920)
