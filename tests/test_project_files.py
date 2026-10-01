"""Project Files rules shared by the Files dock, File Properties and the tools (classes.project_files)."""

import pytest

from classes import project_files as pf


def _frames(folder, names):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"png")
    return folder


def test_detect_sequence_padded_and_unpadded(tmp_path):
    seq = _frames(tmp_path / "a", [f"frame_{i:04d}.png" for i in range(1, 6)])
    d = pf.detect_image_sequence(str(seq / "frame_0003.png"))
    assert d["pattern"] == "frame_%04d.png" and d["fixlen"] and d["path"].endswith("frame_%04d.png")
    assert pf.sequence_frame_numbers(d) == [1, 2, 3, 4, 5]
    loose = _frames(tmp_path / "b", ["img8.jpg", "img9.jpg", "img10.jpg"])
    d2 = pf.detect_image_sequence(str(loose / "img9.jpg"))
    assert d2["pattern"] == "img%d.jpg" and pf.sequence_frame_numbers(d2) == [8, 9, 10]


def test_single_numbered_file_is_not_a_sequence(tmp_path):
    one = _frames(tmp_path / "c", ["photo_0001.png", "other.png"])
    assert pf.detect_image_sequence(str(one / "photo_0001.png")) is None
    assert pf.detect_image_sequence(str(one / "other.png")) is None
    assert pf.detect_image_sequence(str(tmp_path / "clip.mp4")) is None


def test_first_sequence_frame_picks_the_largest_set(tmp_path):
    folder = _frames(tmp_path / "d", ["a_01.png", "a_02.png"] + [f"b_{i:03d}.png" for i in range(10)] + ["logo.png"])
    first, details = pf.first_sequence_frame(str(folder))
    assert first.endswith("b_000.png") and details["pattern"] == "b_%03d.png"
    assert pf.first_sequence_frame(str(tmp_path / "missing")) == (None, None)


@pytest.mark.parametrize("value,expected", [(24, (24, 1)), (23.976, (24000, 1001)), (29.97, (30000, 1001)),
                                            (12.5, (25, 2)), (59.94, (60000, 1001))])
def test_fps_fraction(value, expected):
    assert pf.fps_fraction(value) == expected


def test_apply_sequence_fps_rescales_duration_and_in_out():
    data = {"fps": {"num": 30, "den": 1}, "video_timebase": {"num": 1, "den": 30}, "duration": 2.0,
            "start": 0.5, "end": 1.5}
    pf.apply_sequence_fps(data, 15, 1)
    assert data["fps"] == {"num": 15, "den": 1} and data["video_timebase"] == {"num": 1, "den": 15}
    assert data["duration"] == 4.0 and data["start"] == 1.0 and data["end"] == 3.0


def test_set_in_out_frames_end_is_inclusive():
    data = {"fps": {"num": 25, "den": 1}}
    pf.set_in_out_frames(data, 26, 50)
    assert data["start"] == 1.0 and data["end"] == 2.0


def test_relinked_file_data_keeps_zenvi_keys_and_clamps():
    old = {"id": "F1", "path": "/old/a.mp4", "name": "Intro", "tags": "b-roll", "ai_metadata": {"analyzed": True},
           "start": 2.0, "end": 9.0, "proxy_reader": {"path": "/p.mp4"}, "duration": 10.0}
    reader = {"path": "/new/a.mp4", "duration": 8.0, "width": 1920, "id": "READER"}
    new = pf.relinked_file_data(old, reader, "video")
    assert new["id"] == "F1" and new["path"] == "/new/a.mp4" and new["name"] == "Intro"
    assert new["tags"] == "b-roll" and new["ai_metadata"] == {"analyzed": True}
    assert new["end"] == 8.0 and new["start"] == 2.0 and new["media_type"] == "video"
    gone = pf.relinked_file_data(dict(old, start=8.5, end=9.5), reader, "video")
    assert "start" not in gone and "end" not in gone


def test_relink_keeps_the_optimized_preview_only_for_the_same_media():
    """An optimized preview shows the media it was made from: relinking to other
    footage must not keep playing the old one."""
    old = {"id": "F1", "path": "/old/a.mp4", "duration": 10.0, "proxy_reader": {"path": "/p.mp4"},
           "fingerprint": {"sha256": "aaa"}}
    reader = {"path": "/new/a.mp4", "duration": 10.0}
    moved = pf.relinked_file_data(old, reader, "video", fingerprint={"sha256": "aaa"})
    assert moved["proxy_reader"] == {"path": "/p.mp4"} and moved["fingerprint"] == {"sha256": "aaa"}
    other = pf.relinked_file_data(old, reader, "video", fingerprint={"sha256": "bbb"})
    assert "proxy_reader" not in other and other["fingerprint"] == {"sha256": "bbb"}
    unknown = pf.relinked_file_data(old, reader, "video")
    assert "proxy_reader" not in unknown and "fingerprint" not in unknown


def test_save_file_and_sync_clips_updates_clips_and_deletes_removed_keys(editor):
    from classes.query import File
    fid = editor.add_file("video", duration=10.0)
    cid = editor.add_clip(fid, position=0.0, end=10.0)
    f = File.get(id=fid)
    f.data["start"], f.data["end"] = 1.0, 3.0
    f.save()
    editor.mark()
    f = File.get(id=fid)
    new = dict(f.data, duration=6.0)
    new.pop("start")
    new.pop("end")
    f.data = new
    from classes.tool_handlers import _transaction
    with _transaction(editor.app):
        updated = pf.save_file_and_sync_clips(f, ["start", "end"])
    assert editor.undo_steps_since_mark() == 1
    assert updated == [cid]
    assert "start" not in editor.file(fid) and "end" not in editor.file(fid)
    assert editor.clip(cid)["end"] == 6.0 and editor.clip(cid)["reader"]["duration"] == 6.0
    editor.undo()
    assert editor.file(fid)["start"] == 1.0 and editor.clip(cid)["end"] == 10.0


def test_remove_files_from_project_deletes_clips_then_the_file(editor):
    fid = editor.add_file("video")
    other = editor.add_file("audio")
    c1 = editor.add_clip(fid, position=0.0)
    c2 = editor.add_clip(fid, position=30.0)
    keep = editor.add_clip(other, position=0.0, layer=1000000)
    from classes.query import File
    files, clips = pf.remove_files_from_project([File.get(id=fid)])
    assert files == [fid] and set(clips) == {c1, c2}
    assert editor.file(fid) is None and editor.clip(keep) is not None
    editor.window.generation_queue.cancel_jobs_for_file.assert_called_with(fid)
