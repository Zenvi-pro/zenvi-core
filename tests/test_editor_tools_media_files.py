"""Project Files tools: list/filter, update (rename/tag/relink/in-out/fps), image sequences, removal,
sub-clips and optimized previews (classes.editor_tools.media_files)."""

import json
import os
import threading
from unittest.mock import patch

import pytest

from classes.editor_tools import media_files as mf


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


def _files_by_name(editor, name):
    return [f for f in editor.get("files") if f.get("name") == name]


def _real_file(tmp_path, name, data=b"media"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return str(p)


# ---------------------------------------------------------------------------
# list_project_files_tool
# ---------------------------------------------------------------------------

def test_list_filters_by_kind_text_tags_and_usage(editor, tmp_path):
    v = editor.add_file("video", path=_real_file(tmp_path, "drone_shot.mp4"), tags="b-roll, aerial")
    a = editor.add_file("audio", path=_real_file(tmp_path, "music.mp3"))
    img = editor.add_file("image", path=str(tmp_path / "gone.jpg"), name="Logo")
    editor.add_clip(v, position=0.0)

    r = _receipt(editor.call("list_project_files_tool"))
    assert r["matched"] == 3 and r["total_files"] == 3
    by_id = {f["file_id"]: f for f in r["files"]}
    assert by_id[v]["clip_count"] == 1 and by_id[v]["tags"] == ["b-roll", "aerial"]
    assert by_id[v]["optimized_preview"] == "none" and by_id[img]["missing"] is True
    assert [f["file_id"] for f in _receipt(editor.call("list_project_files_tool", usage="unused"))["files"]] == [a, img]
    assert [f["file_id"] for f in _receipt(editor.call("list_project_files_tool", media_type="audio"))["files"]] == [a]
    assert [f["file_id"] for f in _receipt(editor.call("list_project_files_tool", query="drone"))["files"]] == [v]
    assert [f["file_id"] for f in _receipt(editor.call("list_project_files_tool", tags=["B-Roll"]))["files"]] == [v]
    assert [f["file_id"] for f in _receipt(editor.call("list_project_files_tool", missing_only=True))["files"]] == [img]
    assert editor.undo_steps_since_mark() == 0


def test_list_full_detail_and_unknown_ids(editor):
    v = editor.add_file("video")
    full = _receipt(editor.call("list_project_files_tool", file_ids=[v], detail="full"))["files"][0]
    assert full["fps"] == "30/1" and full["width"] == 1280 and full["frames"] == 737
    assert "video_codec" in full and "channel_layout" in full
    assert editor.call("list_project_files_tool", file_ids=["NOPE"]).startswith("Error")


def test_list_marks_image_sequences_and_titles(editor, tmp_path):
    folder = tmp_path / "seq"
    folder.mkdir()
    (folder / "f_0001.png").write_bytes(b"x")
    s = editor.add_file("video", path=str(folder / "f_%04d.png"))
    t = editor.add_file("image", path=str(tmp_path / "title.svg"))
    r = _receipt(editor.call("list_project_files_tool", media_type="image_sequence"))
    assert [f["file_id"] for f in r["files"]] == [s] and r["files"][0]["missing"] is False
    assert _receipt(editor.call("list_project_files_tool", media_type="title"))["files"][0]["file_id"] == t


# ---------------------------------------------------------------------------
# update_project_files_tool
# ---------------------------------------------------------------------------

def test_rename_is_one_undo_step_and_can_relabel_clips(editor):
    v = editor.add_file("video")
    c = editor.add_clip(v)
    out = editor.call("update_project_files_tool", file_ids=[v], name="Intro")
    assert _receipt(out)["changed"] is True
    assert editor.file(v)["name"] == "Intro" and editor.clip(c)["title"] == "sample_video.mp4"
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert "name" not in editor.file(v) or editor.file(v)["name"] != "Intro"
    editor.mark()
    _receipt(editor.call("update_project_files_tool", file_query="sample_video", name="Hook", rename_clips=True))
    assert editor.clip(c)["title"] == "Hook" and editor.undo_steps_since_mark() == 1


def test_tags_on_many_files_add_remove_replace(editor):
    a = editor.add_file("video", tags="interview")
    b = editor.add_file("audio")
    _receipt(editor.call("update_project_files_tool", file_ids=[a, b], add_tags=["b-roll", "Interview"]))
    assert editor.file(a)["tags"] == "interview, b-roll" and editor.file(b)["tags"] == "b-roll, Interview"
    assert editor.undo_steps_since_mark() == 1
    editor.mark()
    _receipt(editor.call("update_project_files_tool", file_ids=[a], remove_tags=["INTERVIEW"]))
    assert editor.file(a)["tags"] == "b-roll"
    _receipt(editor.call("update_project_files_tool", file_ids=[b], tags=[]))
    assert editor.file(b)["tags"] == ""


def test_update_refusals_and_noop(editor):
    a = editor.add_file("video", name="A")
    b = editor.add_file("video")
    for args in ({"file_ids": [a, b], "name": "X"},
                 {"file_ids": [a]},
                 {"file_ids": [a], "fps": 24},
                 {"file_ids": [a], "rename_clips": True},
                 {"file_ids": [a], "source_start_seconds": 5, "source_end_seconds": 2},
                 {"file_ids": [a], "source_end_seconds": 999},
                 {"file_ids": [a], "path": "/definitely/missing.mp4"},
                 {"file_query": "sample_video"},
                 {"file_ids": ["NOPE"], "name": "X"}):
        out = editor.call("update_project_files_tool", **args)
        assert out.startswith("Error"), (args, out)
    assert editor.undo_steps_since_mark() == 0
    r = _receipt(editor.call("update_project_files_tool", file_ids=[a], name="A"))
    assert r["changed"] is False and editor.undo_steps_since_mark() == 0


def test_in_out_and_reset(editor):
    v = editor.add_file("video", duration=20.0)
    r = _receipt(editor.call("update_project_files_tool", file_ids=[v], source_start_seconds=2.0,
                             source_end_seconds=5.5))
    assert editor.file(v)["start"] == pytest.approx(2.0) and editor.file(v)["end"] == pytest.approx(5.5)
    assert r["files"][0]["duration"] == pytest.approx(3.5)
    editor.mark()
    _receipt(editor.call("update_project_files_tool", file_ids=[v], reset_in_out=True))
    assert "start" not in editor.file(v) and "end" not in editor.file(v)
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.file(v)["start"] == pytest.approx(2.0)


def test_sequence_fps_rescales_and_clamps_clips(editor, tmp_path):
    s = editor.add_file("video", path=str(tmp_path / "f_%04d.png"), duration=2.0)
    c = editor.add_clip(s, end=2.0)
    _receipt(editor.call("update_project_files_tool", file_ids=[s], fps=60))
    assert editor.file(s)["fps"] == {"num": 60, "den": 1} and editor.file(s)["duration"] == pytest.approx(1.0)
    assert editor.clip(c)["end"] == pytest.approx(1.0) and editor.clip(c)["reader"]["duration"] == pytest.approx(1.0)
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.file(s)["fps"] == {"num": 30, "den": 1} and editor.clip(c)["end"] == pytest.approx(2.0)


def _fake_reader(path):
    return {"path": path, "duration": 12.0, "video_length": "360", "fps": {"num": 30, "den": 1},
            "has_video": True, "has_audio": True, "width": 1920, "height": 1080, "has_single_image": False,
            "media_type": "video"}


def test_relink_keeps_tags_updates_clips_and_reports_content(editor, tmp_path):
    old = _real_file(tmp_path, "old/interview.mp4", b"AAAA")
    v = editor.add_file("video", path=old, tags="interview", name="Interview")
    c = editor.add_clip(v, end=5.0)
    from classes.media_fingerprint import fingerprint
    from classes.query import File
    f = File.get(id=v)
    f.data["fingerprint"] = fingerprint(old)
    f.save()
    editor.mark()
    moved = _real_file(tmp_path, "new/deep/interview.mp4", b"AAAA")
    with patch.object(mf, "_probe_reader", _fake_reader):
        r = _receipt(editor.call("update_project_files_tool", file_ids=[v], search_folder=str(tmp_path / "new")))
    assert editor.file(v)["path"] == moved and editor.file(v)["tags"] == "interview"
    assert editor.file(v)["name"] == "Interview" and editor.file(v)["duration"] == 12.0
    assert editor.clip(c)["reader"]["path"] == moved
    assert r["files"][0]["same_content"] is True and editor.undo_steps_since_mark() == 1
    other = _real_file(tmp_path, "else/interview.mp4", b"BBBB")
    with patch.object(mf, "_probe_reader", _fake_reader):
        out2 = editor.call("update_project_files_tool", file_ids=[v], path=other)
    assert _receipt(out2)["files"][0]["same_content"] is False and "content differs" in out2
    editor.undo()
    assert editor.file(v)["path"] == moved
    with patch.object(mf, "_probe_reader", _fake_reader):
        (tmp_path / "nowhere").mkdir()
        out = editor.call("update_project_files_tool", file_ids=[v], search_folder=str(tmp_path / "nowhere"))
    assert out.startswith("Error")


# ---------------------------------------------------------------------------
# import_image_sequence_tool
# ---------------------------------------------------------------------------

def _sequence(tmp_path, n=10, name="frame_%04d.png"):
    folder = tmp_path / "frames"
    folder.mkdir(exist_ok=True)
    for i in range(1, n + 1):
        (folder / (name % i)).write_bytes(b"png")
    return folder


def _fake_add_files(editor):
    calls = []

    def add_files(files, image_seq_details=None, quiet=False, prevent_image_seq=False,
                  prevent_recent_folder=False, skip_indexing=False):
        from classes.query import File
        calls.append({"files": list(files), "seq": image_seq_details, "quiet": quiet,
                      "skip_indexing": skip_indexing})
        fps = image_seq_details["fps"]
        f = File()
        f.data = {"path": image_seq_details["path"], "media_type": "video", "fps": dict(fps),
                  "video_timebase": {"num": fps["den"], "den": fps["num"]},
                  "duration": 10 * fps["den"] / fps["num"], "width": 640, "height": 360, "video_length": "10"}
        f.save()
        return [f]

    editor.window.files_model.add_files = add_files
    return calls


def test_import_sequence_from_folder_at_24fps(editor, tmp_path):
    folder = _sequence(tmp_path)
    calls = _fake_add_files(editor)
    r = _receipt(editor.call("import_image_sequence_tool", path=str(folder), fps=24, name="Intro frames"))
    assert r["frames"] == 10 and r["fps"] == "24/1" and r["path"].endswith("frame_%04d.png")
    assert calls[0]["files"][0].endswith("frame_0001.png") and calls[0]["quiet"] is True
    assert calls[0]["seq"]["fps"] == {"num": 24, "den": 1} and calls[0]["seq"]["length_multiplier"] == 1
    assert editor.file(r["file_id"])["name"] == "Intro frames"
    assert editor.undo_steps_since_mark() == 1
    again = _receipt(editor.call("import_image_sequence_tool", path=str(folder / "frame_0005.png")))
    assert again["changed"] is False and again["file_id"] == r["file_id"]


def test_import_sequence_defaults_to_project_fps_and_refuses_non_sequences(editor, tmp_path):
    folder = _sequence(tmp_path, n=3)
    calls = _fake_add_files(editor)
    _receipt(editor.call("import_image_sequence_tool", path=str(folder / "frame_0002.png")))
    assert calls[0]["seq"]["fps"] == {"num": 30, "den": 1}
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "photo_0001.jpg").write_bytes(b"x")
    editor.mark()
    for p in (str(lone), str(lone / "photo_0001.jpg"), str(tmp_path / "nope")):
        assert editor.call("import_image_sequence_tool", path=p).startswith("Error")
    assert editor.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# remove_files_from_project_tool
# ---------------------------------------------------------------------------

def test_remove_unused_touches_no_clip(editor):
    used = editor.add_file("video")
    unused = editor.add_file("audio")
    c = editor.add_clip(used)
    r = _receipt(editor.call("remove_files_from_project_tool", scope="unused"))
    assert r["removed_file_ids"] == [unused] and r["removed_clip_ids"] == []
    assert editor.file(used) and editor.clip(c) and editor.file(unused) is None
    assert editor.undo_steps_since_mark() == 1


def test_remove_used_file_needs_confirm_and_undo_restores_both(editor):
    v = editor.add_file("video")
    c1 = editor.add_clip(v, position=0.0)
    c2 = editor.add_clip(v, position=30.0, layer=2000000)
    out = editor.call("remove_files_from_project_tool", file_ids=[v])
    assert out.startswith("Error") and "confirm_remove_clips" in out and "2 timeline clip" in out
    assert editor.file(v) and editor.undo_steps_since_mark() == 0
    r = _receipt(editor.call("remove_files_from_project_tool", file_ids=[v], confirm_remove_clips=True))
    assert set(r["removed_clip_ids"]) == {c1, c2} and editor.file(v) is None and editor.clips() == []
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.file(v) and editor.clip(c1) and editor.clip(c2)
    editor.redo()
    assert editor.file(v) is None and editor.clips() == []


def test_remove_refuses_locked_tracks_and_bad_targets(editor):
    v = editor.add_file("video")
    editor.add_clip(v, layer=2000000)
    editor.lock_track(2000000)
    assert "locked" in editor.call("remove_files_from_project_tool", file_ids=[v], confirm_remove_clips=True)
    assert editor.call("remove_files_from_project_tool", scope="unused", file_ids=[v]).startswith("Error")
    assert editor.call("remove_files_from_project_tool").startswith("Error")
    assert editor.undo_steps_since_mark() == 0
    r = _receipt(editor.call("remove_files_from_project_tool", scope="unused"))
    assert r["changed"] is False


def test_remove_missing_scope(editor, tmp_path):
    here = editor.add_file("video", path=_real_file(tmp_path, "here.mp4"))
    gone = editor.add_file("video", path=str(tmp_path / "gone.mp4"))
    r = _receipt(editor.call("remove_files_from_project_tool", scope="missing"))
    assert r["removed_file_ids"] == [gone] and editor.file(here)


# ---------------------------------------------------------------------------
# create_subclips_tool
# ---------------------------------------------------------------------------

def test_create_several_subclips_in_one_undo_step(editor):
    v = editor.add_file("video", duration=90.0)
    r = _receipt(editor.call("create_subclips_tool", file_id=v, subclips=[
        {"source_start_seconds": 5, "source_end_seconds": 12, "name": "Hook"},
        {"source_start_seconds": 70, "source_end_seconds": 80.5}]))
    subs = r["subclips"]
    assert [s["name"] for s in subs] == ["Hook", "sample_video (01:10;00 to 01:20;15)"]
    assert editor.file(subs[0]["file_id"])["start"] == 5 and editor.file(subs[0]["file_id"])["end"] == 12
    assert editor.file(subs[1]["file_id"])["path"] == editor.file(v)["path"]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.file(subs[0]["file_id"]) is None and editor.file(subs[1]["file_id"]) is None


def test_subclip_refusals(editor):
    v = editor.add_file("video", duration=10.0)
    img = editor.add_file("image")
    for args in ({"file_id": v, "subclips": [{"source_start_seconds": 5, "source_end_seconds": 20}]},
                 {"file_id": v, "subclips": [{"source_start_seconds": 5, "source_end_seconds": 5}]},
                 {"file_id": v, "subclips": []},
                 {"file_id": img, "subclips": [{"source_start_seconds": 0, "source_end_seconds": 2}]},
                 {"file_id": "NOPE", "subclips": [{"source_start_seconds": 0, "source_end_seconds": 2}]},
                 {"file_id": v, "subclips": [{"source_start_seconds": 0}]}):
        assert editor.call("create_subclips_tool", **args).startswith("Error"), args
    assert editor.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# manage_optimized_previews_tool
# ---------------------------------------------------------------------------

@pytest.fixture
def proxies(editor, tmp_path):
    from classes import proxy_service
    from classes.proxy_service import ProxyService

    class _Service(ProxyService):
        def __init__(self, win):
            self.win = win
            self._executor = None
            self._jobs = {}
            self._lock = threading.RLock()
            self._ensure_executor()

    p = patch.object(proxy_service, "get_app", return_value=editor.app)
    p.start()
    root = tmp_path / "optimized"
    service = _Service(editor.window)
    service._proxy_root = lambda: str(root)
    service.apply_runtime_updates_for_files = lambda ids: False
    service._reader_json_for_path = lambda path, fid: {"path": path, "id": fid, "width": 640, "height": 360}
    editor.window.proxy_service = service
    yield service
    p.stop()
    service.shutdown()


def test_optimize_starts_a_job_and_waits_for_it(editor, proxies, tmp_path):
    v = editor.add_file("video", path=_real_file(tmp_path, "fourk.mp4"))
    release = threading.Event()

    def build(file_id, snapshot):
        release.wait(5)
        out = proxies._reserved_or_computed_proxy_output_path(file_id, snapshot)
        with open(out, "wb") as fh:
            fh.write(b"proxy")
        return {"path": out, "id": file_id, "width": 1280, "height": 720}

    proxies._build_proxy_reader = build
    proxies.proxy_generated.emit = proxies._on_proxy_generated
    r = _receipt(editor.call("manage_optimized_previews_tool", action="optimize", file_ids=[v]))
    assert r["started"] == [v] and r["files"][0]["state"] in ("queued", "running")
    assert editor.undo_steps_since_mark() == 0
    status = _receipt(editor.call("manage_optimized_previews_tool", action="status"))
    assert status["files"][0]["state"] in ("queued", "running")
    release.set()
    for _ in range(100):
        if not proxies.get_active_job_for_file(v):
            break
        threading.Event().wait(0.05)
    assert editor.file(v)["proxy_reader"]["width"] == 1280
    again = _receipt(editor.call("manage_optimized_previews_tool", action="optimize", file_ids=[v]))
    assert again["changed"] is False


def test_optimize_refuses_non_video_and_missing_media(editor, proxies, tmp_path):
    a = editor.add_file("audio")
    gone = editor.add_file("video", path=str(tmp_path / "gone.mp4"))
    assert "only video" in editor.call("manage_optimized_previews_tool", file_ids=[a])
    assert "missing" in editor.call("manage_optimized_previews_tool", file_ids=[gone])
    assert "confirm" in editor.call("manage_optimized_previews_tool", action="clear_all")


def test_unlink_delete_and_link(editor, proxies, tmp_path):
    src = _real_file(tmp_path, "clip.mp4")
    v = editor.add_file("video", path=src)
    copy_path = _real_file(tmp_path, "optimized/clip_proxy.mp4")
    from classes.query import File
    f = File.get(id=v)
    f.data["proxy_reader"] = {"path": copy_path, "id": v}
    f.save()
    editor.mark()
    r = _receipt(editor.call("manage_optimized_previews_tool", action="unlink", file_ids=[v]))
    assert "proxy_reader" not in editor.file(v) and os.path.exists(copy_path) and r["kept_paths"] == [copy_path]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.file(v)["proxy_reader"]["path"] == copy_path
    editor.mark()
    r = _receipt(editor.call("manage_optimized_previews_tool", action="delete", file_ids=[v]))
    assert r["deleted"] == 1 and not os.path.exists(copy_path) and "proxy_reader" not in editor.file(v)
    assert editor.undo_steps_since_mark() == 1
    # Link back from a folder (matched by file name)
    folder = tmp_path / "elsewhere"
    linked = _real_file(tmp_path, "elsewhere/clip.mp4")
    r = _receipt(editor.call("manage_optimized_previews_tool", action="link", file_ids=[v], folder=str(folder)))
    assert editor.file(v)["proxy_reader"]["path"] == linked and r["linked"][0]["proxy_path"] == linked
    assert "no optimized copy" in editor.call("manage_optimized_previews_tool", action="link", file_ids=[v],
                                              folder=str(tmp_path / "optimized"))
    assert _receipt(editor.call("manage_optimized_previews_tool", action="cancel", file_ids=[v]))["changed"] is False


def test_clear_all_deletes_the_project_copies(editor, proxies, tmp_path):
    v = editor.add_file("video", path=_real_file(tmp_path, "a.mp4"))
    inside = _real_file(tmp_path, "optimized/a_proxy.mp4")
    stray = _real_file(tmp_path, "optimized/old.mp4")
    from classes.query import File
    f = File.get(id=v)
    f.data["proxy_reader"] = {"path": inside, "id": v}
    f.save()
    editor.mark()
    r = _receipt(editor.call("manage_optimized_previews_tool", action="clear_all", confirm=True))
    assert r["unlinked"] == 1 and r["deleted"] == 2 and not os.path.exists(inside) and not os.path.exists(stray)
    assert "proxy_reader" not in editor.file(v) and editor.undo_steps_since_mark() == 1


def test_stills_report_no_duration_and_audio_layout(editor):
    img = editor.add_file("image")
    aud = editor.add_file("audio", channel_layout=3)
    rows = {f["file_id"]: f for f in _receipt(editor.call("list_project_files_tool", detail="full"))["files"]}
    assert rows[img]["duration"] is None and rows[img]["media_duration"] is None
    assert rows[img]["channel_layout"] == "none" and rows[aud]["channel_layout"] == "stereo"
