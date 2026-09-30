"""classes.timeline_ops: the gap / ripple / align / Add-to-Timeline rules the menus and the tools share."""

import pytest

from classes import timeline_ops
from classes.query import Clip, Transition
from timeline_edit_fakes import fake_openshot


class _Updates:
    def __init__(self, tid=None):
        self.transaction_id = tid


def test_joined_transaction_owns_a_fresh_id_and_clears_it():
    updates = _Updates()
    with timeline_ops.joined_transaction(updates) as tid:
        assert tid and updates.transaction_id == tid
    assert updates.transaction_id is None


def test_joined_transaction_joins_and_keeps_the_callers_id():
    """An agent tool's group must survive a menu handler that joins it."""
    updates = _Updates("outer")
    with timeline_ops.joined_transaction(updates) as tid:
        assert tid == "outer"
    assert updates.transaction_id == "outer"


def test_joined_transaction_clears_its_own_id_when_the_body_raises():
    updates = _Updates()
    with pytest.raises(RuntimeError):
        with timeline_ops.joined_transaction(updates):
            raise RuntimeError("boom")
    assert updates.transaction_id is None


def _positions(editor, ids):
    return [round(editor.clip(i)["position"], 3) for i in ids]


def test_gaps_are_found_like_the_track_menu(editor):
    f = editor.add_file("video", duration=10)
    a = editor.add_clip(f, position=2.0, end=3.0)   # leading gap 0-2, then 2-5
    editor.add_clip(f, position=5.0, end=2.0)        # 5-7, no gap after a
    editor.add_clip(f, position=9.0, end=1.0)        # gap 7-9
    assert timeline_ops.list_gaps(1000000) == [(0.0, 2.0), (7.0, 9.0)]
    assert timeline_ops.list_gaps(1000000, after_seconds=6.0) == [(7.0, 9.0)]
    assert timeline_ops.find_gap(1000000, 4.0) == (7.0, 9.0)
    assert timeline_ops.find_gap(1000000, 9.5) is None
    assert editor.clip(a)["position"] == 2.0


def test_close_gap_moves_only_later_items(editor):
    f = editor.add_file("video", duration=10)
    a = editor.add_clip(f, position=0.0, end=2.0)
    b = editor.add_clip(f, position=4.0, end=2.0)
    c = editor.add_clip(f, position=8.0, end=2.0)
    moved = timeline_ops.close_gap(2.0, 4.0, 1000000)
    assert {m.id for m in moved} == {b, c}
    assert _positions(editor, [a, b, c]) == [0.0, 2.0, 6.0]


def test_close_all_gaps_moves_overlapping_groups_together(editor):
    f = editor.add_file("video", duration=10)
    a = editor.add_clip(f, position=1.0, end=2.0)            # 1-3
    b = editor.add_clip(f, position=5.0, end=3.0)            # 5-8
    c = editor.add_clip(f, position=7.0, end=2.0)            # 7-9 overlaps b (crossfade)
    d = editor.add_clip(f, position=12.0, end=1.0)           # 12-13
    timeline_ops.close_all_gaps(0.0, 1000000)
    assert _positions(editor, [a, b, c, d]) == [0.0, 2.0, 4.0, 6.0]


def test_close_gap_at_never_pushes_an_overlapping_clip_before_the_hole(editor):
    """Regression: ripple delete of a clip crossfading into the next one moved
    the next one by the full length, to a negative position."""
    f = editor.add_file("video", duration=20)
    b = editor.add_clip(f, position=9.0, end=10.0)   # 9-19, overlapped the deleted 0-10 clip
    c = editor.add_clip(f, position=19.0, end=5.0)
    moved = timeline_ops.close_gap_at(1000000, 0.0, 10.0)
    assert {m.id for m in moved} == {b, c}
    assert _positions(editor, [b, c]) == [0.0, 10.0]


def test_close_gap_at_plain_ripple(editor):
    f = editor.add_file("video", duration=20)
    b = editor.add_clip(f, position=12.0, end=3.0)
    timeline_ops.close_gap_at(1000000, 0.0, 10.0)
    assert editor.clip(b)["position"] == 2.0


def test_shift_after_opens_room(editor):
    f = editor.add_file("video", duration=20)
    a = editor.add_clip(f, position=0.0, end=3.0)
    b = editor.add_clip(f, position=3.0, end=3.0)
    timeline_ops.shift_after(1000000, 3.0, 2.0)
    assert _positions(editor, [a, b]) == [0.0, 5.0]


def test_aligned_positions():
    datas = [{"id": "a", "position": 2.0, "start": 0.0, "end": 4.0},
             {"id": "b", "position": 5.0, "start": 1.0, "end": 2.0}]
    assert timeline_ops.aligned_positions(datas, False) == {"a": 2.0, "b": 2.0}
    right = timeline_ops.aligned_positions(datas, True)
    assert right == {"a": 2.0, "b": 5.0}  # ends: a at 6.0 is the latest; b ends at 6.0 already
    assert timeline_ops.aligned_positions(datas, False, 10.0) == {"a": 10.0, "b": 10.0}


@pytest.fixture
def placing(editor, monkeypatch):
    monkeypatch.setattr(timeline_ops, "openshot", fake_openshot())
    return editor


def test_place_files_back_to_back_with_subclip_and_override(placing):
    from classes.query import File
    editor = placing
    v = editor.add_file("video", duration=10)
    sub = editor.add_file("video", duration=10, start=2.0, end=5.0)
    img = editor.add_file("image")
    entries = [{"file": File.get(id=v)}, {"file": File.get(id=sub)}, {"file": File.get(id=img)},
               {"file": File.get(id=v), "start": 1.0, "end": 2.5}]
    clip_ids, tran_ids = timeline_ops.place_files(entries, 4.0, 2000000, image_length=3.0)
    assert tran_ids == []
    got = [(editor.clip(c)["position"], editor.clip(c)["start"], editor.clip(c)["end"]) for c in clip_ids]
    assert got == [(4.0, 0, 10), (14.0, 2.0, 5.0), (17.0, 0, 3.0), (20.0, 1.0, 2.5)]
    assert all(editor.clip(c)["layer"] == 2000000 for c in clip_ids)


def test_place_files_fades_overlap_and_key_alpha(placing):
    from classes.query import File
    editor = placing
    v = editor.add_file("video", duration=6)
    entries = [{"file": File.get(id=v)}, {"file": File.get(id=v)}]
    clip_ids, _ = timeline_ops.place_files(entries, 0.0, 1000000, fade=timeline_ops.FADE_IN_OUT, fade_length=1.0)
    assert [editor.clip(c)["position"] for c in clip_ids] == [0.0, 5.0]
    alpha = [p["co"] for p in editor.clip(clip_ids[1])["alpha"]["Points"]]
    assert {"X": 1.0, "Y": 0.0} in alpha and {"X": 31.0, "Y": 1.0} in alpha and {"X": 181.0, "Y": 0.0} in alpha


def test_place_files_transitions_between_clips_only(placing):
    from classes.query import File
    editor = placing
    v = editor.add_file("video", duration=6)
    entries = [{"file": File.get(id=v)} for _ in range(3)]
    clip_ids, tran_ids = timeline_ops.place_files(entries, 0.0, 1000000, transition_path="/t/fade.svg",
                                                  transition_length=1.0, transition_first_clip=False)
    assert [editor.clip(c)["position"] for c in clip_ids] == [0.0, 5.0, 10.0]
    trans = [Transition.get(id=t).data for t in tran_ids]
    assert [(t["position"], t["end"], t["reader"]["path"]) for t in trans] == [(5.0, 1.0, "/t/fade.svg"),
                                                                            (10.0, 1.0, "/t/fade.svg")]


def test_place_files_dialog_default_also_wipes_in_the_first_clip(placing):
    from classes.query import File
    editor = placing
    v = editor.add_file("video", duration=6)
    _clips, tran_ids = timeline_ops.place_files([{"file": File.get(id=v)}] * 2, 0.0, 1000000,
                                                transition_path="/t/fade.svg", transition_length=1.0)
    assert len(tran_ids) == 2
    assert len(Clip.filter()) == 2
