"""project-export: export presets, export_video, export settings, save frame, export files."""

import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

from classes import info


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


class _Settings:
    class actionType:
        IMPORT, EXPORT, LOAD, SAVE = range(4)

    def __init__(self, folder):
        self.folder = folder

    def get(self, key, default=None):
        return {"default-image-length": 10.0}.get(key, default)

    def getDefaultPath(self, *_a):
        return self.folder

    def setDefaultPath(self, *_a):
        pass


@pytest.fixture
def studio(editor, tmp_path, monkeypatch):
    """1080p30 project with a 10 s clip, real presets/profiles, a fake headless renderer."""
    from classes import project_profile

    monkeypatch.setattr(info, "USER_PROFILES_PATH", str(tmp_path / "no_user_profiles"))
    monkeypatch.setattr(info, "USER_PRESETS_PATH", str(tmp_path / "no_user_presets"))
    project_profile.invalidate_catalog()
    editor.store._data.update(profile="FHD 1080p 30 fps", width=1920, height=1080,
                              display_ratio={"num": 16, "den": 9})
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    editor.app.get_settings.return_value = _Settings(str(out_dir))
    editor.app.get_settings.side_effect = None
    editor.app._tr = lambda s: s
    editor.clip_id = editor.add_clip(editor.add_file("video", duration=10.0), position=1.0)

    calls = []
    export_mod = types.ModuleType("windows.export")

    def export_video_headless(path, video, audio, export_type, video_bitrate_text=None,
                              profile_path_for_rescale=None):
        calls.append(dict(path=path, video=video, audio=audio, export_type=export_type,
                          video_bitrate_text=video_bitrate_text, profile_path=profile_path_for_rescale))
        if export_type == "Image Sequence":
            for n in (1, 2):
                with open(path % n, "wb") as fh:
                    fh.write(b"\x89PNG")
        else:
            with open(path, "wb") as fh:
                fh.write(b"\x00" * 2048)
        return None

    export_mod.export_video_headless = export_video_headless
    export_mod.Export = MagicMock()
    monkeypatch.setitem(sys.modules, "windows.export", export_mod)
    editor.renders = calls
    editor.out_dir = out_dir
    yield editor
    project_profile.invalidate_catalog()


# --- presets --------------------------------------------------------------

def test_presets_resolve_by_title_alias_and_social_name(studio):
    from classes.editor_tools.project_export_render import resolve_preset
    assert resolve_preset("Instagram Reels")["title"] == "Instagram Reels"
    assert resolve_preset("instagram_reel")["title"] == "Instagram Reels"
    assert resolve_preset("tiktok")["title"] == "TikTok"
    assert resolve_preset("gif")["title"] == "GIF (animated)"
    assert resolve_preset("MP3")["export_to"] == "Audio Only"
    assert resolve_preset("youtube_4k")["title"] == "YouTube (4K)"
    assert resolve_preset("prores")["vcodec"] == "prores_ks"
    with pytest.raises(Exception, match="no export preset"):
        resolve_preset("betamax")


def test_list_presets_has_codecs_bitrates_and_profiles(studio):
    data = _receipt(studio.call("list_export_presets_tool", category="Web"))
    reels = next(p for p in data["presets"] if p["title"] == "Instagram Reels")
    assert reels["video_codec"] == "libx264" and reels["qualities"]["high"]["video"] == "5.5 Mb/s"
    assert "FHD Vertical 1080p 30 fps" in reels["profiles"]
    data = _receipt(studio.call("list_export_presets_tool", query="gif"))
    assert [p["title"] for p in data["presets"]] == ["GIF (animated)"]
    assert data["presets"][0]["profiles"] == "any"


# --- export plans ---------------------------------------------------------

def test_default_export_is_mp4_h264_high_over_the_whole_timeline(studio):
    from classes.editor_tools.project_export_render import build_export_plan
    plan = build_export_plan()
    assert (plan["preset"], plan["quality"], plan["export_type"]) == ("MP4 (h.264)", "High", "video_audio")
    assert plan["video_bitrate"] == "20 crf" and plan["acodec"] == "aac"
    assert (plan["start_seconds"], plan["end_seconds"]) == (1.0, 11.0), "first clip to last clip"
    assert (plan["start_frame"], plan["end_frame"]) == (31, 330)
    assert plan["path"] == os.path.join(str(studio.out_dir), "Untitled Project.mp4")


def test_reels_preset_on_a_landscape_project_warns_about_bars(studio):
    from classes.editor_tools.project_export_render import build_export_plan
    plan = build_export_plan(preset="Instagram Reels", quality="med")
    assert (plan["width"], plan["height"], plan["fps"]) == (1080, 1920, 30.0)
    assert plan["video_bitrate"] == "4.5 Mb/s" and plan["audio_bitrate"] == "128 kb/s"
    assert any("re-frames every clip" in n for n in plan["notes"])
    studio.store._data.update(profile="FHD Vertical 1080p 30 fps", width=1080, height=1920,
                              display_ratio={"num": 9, "den": 16})
    plan = build_export_plan(preset="Instagram Reels")
    assert plan["profile"] == "FHD Vertical 1080p 30 fps" and plan["notes"] == []


def test_all_formats_bitrates_follow_size_and_fps(studio):
    from classes.editor_tools.project_export_render import build_export_plan
    plan = build_export_plan(preset="MOV (mpeg4)", quality="high")
    assert plan["video_bitrate"] == "%.2f Mb/s" % (1920 * 1080 * 30 * 0.12 / 1e6)


def test_gif_mp3_wav_and_image_sequence_types(studio):
    from classes.editor_tools.project_export_render import build_export_plan
    gif = build_export_plan(preset="GIF", width=480, height=270)
    assert gif["export_type"] == "video_only" and gif["path"].endswith(".gif") and gif["acodec"] == ""
    mp3 = build_export_plan(preset="MP3")
    assert mp3["export_type"] == "audio_only" and mp3["path"].endswith(".mp3") and mp3["vcodec"] == ""
    wav = build_export_plan(preset="MP3", container="wav")
    assert wav["acodec"] == "pcm_s16le" and wav["path"].endswith(".wav")
    seq = build_export_plan(export_type="image_sequence", image_format="jpg", file_name="frames")
    assert seq["path"].endswith("frames-%05d.jpg") and seq["vcodec"] == "mjpeg"


def test_ranges_and_paths(studio):
    from classes.editor_tools._base import ToolError
    from classes.editor_tools.project_export_render import build_export_plan
    plan = build_export_plan(start=2.0, end=7.0, output_path="~/zenvi-test/reel")
    assert (plan["range"], plan["start_frame"], plan["end_frame"]) == ("custom", 61, 210)
    assert plan["path"] == os.path.normpath(os.path.expanduser("~/zenvi-test/reel.mp4"))
    assert build_export_plan(output_path=str(studio.out_dir / "a.mov"))["vformat"] == "mov"
    # A .gif path with an MP4 preset would hand the GIF muxer H.264/AAC.
    with pytest.raises(ToolError, match="preset='GIF'"):
        build_export_plan(output_path=str(studio.out_dir / "a.gif"))
    assert build_export_plan(preset="GIF", output_path=str(studio.out_dir / "a.gif"))["vcodec"] == "gif"
    with pytest.raises(ToolError, match="before it starts"):
        build_export_plan(start=5, end=3)
    with pytest.raises(ToolError, match="after the last clip"):
        build_export_plan(start=50, end=60)
    with pytest.raises(ToolError, match="no clips are selected"):
        build_export_plan(range_mode="selection")
    studio.window.selected_clips = [studio.clip_id]
    assert build_export_plan(range_mode="selection")["start_seconds"] == 1.0
    with pytest.raises(ToolError, match="even"):
        build_export_plan(width=1001, height=500)
    with pytest.raises(ToolError, match="video_bitrate"):
        build_export_plan(video_bitrate="fast")


# --- export_video ---------------------------------------------------------

def test_export_renders_headlessly_with_the_planned_settings(studio):
    data = _receipt(studio.call("export_video_tool", preset="YouTube", start=1, end=6, file_name="yt"))
    call = studio.renders[-1]
    assert call["path"] == str(studio.out_dir / "yt.mp4") and call["export_type"] == "Video & Audio"
    assert call["video"]["start_frame"] == 31 and call["video"]["end_frame"] == 180
    assert call["video"]["video_bitrate"] == "12 Mb/s" and call["video_bitrate_text"] == "12 mb/s"
    assert call["audio"]["acodec"] == "aac" and call["audio"]["sample_rate"] == 48000
    assert data["size_bytes"] == 2048 and data["duration_seconds"] == 5.0
    assert studio.undo_steps_since_mark() == 0
    out = studio.call("export_video_tool", preset="YouTube", start=1, end=6, file_name="yt")
    assert out.startswith("Error") and "overwrite" in out
    _receipt(studio.call("export_video_tool", preset="YouTube", start=1, end=6, file_name="yt", overwrite=True))


def test_export_renders_on_a_qthread_not_the_gui_thread(studio, monkeypatch):
    """A render marshalled onto the GUI thread freezes the editor for the whole export."""
    from classes.editor_tools import project_export_render as render

    where = []
    monkeypatch.setattr(render, "on_main", lambda func, *a, **k: (where.append("gui"), func(*a), where.pop())[1])
    monkeypatch.setattr(render, "run_on_qthread", lambda func, *a: (where.append("qthread"), func(), where.pop())[1])
    headless = sys.modules["windows.export"].export_video_headless
    seen = []
    sys.modules["windows.export"].export_video_headless = lambda *a, **k: (seen.append(list(where)), headless(*a, **k))[1]
    _receipt(studio.call("export_video_tool", file_name="off_gui"))
    assert seen == [["qthread"]]


def test_a_render_that_times_out_is_cancelled(studio, monkeypatch):
    """Left running, it would write over the next export of the same file."""
    from classes.editor_tools import project_export_render as render
    from classes.editor_tools._base import ToolError

    def timed_out(func, *a):
        raise ToolError("the render did not finish within 1 s")

    monkeypatch.setattr(render, "run_on_qthread", timed_out)
    cancelled = []
    sys.modules["windows.export"].cancel_headless_exports = lambda *a, **k: cancelled.append(True) or True
    out = studio.call("export_video_tool", file_name="slow")
    assert out.startswith("Error") and "did not finish" in out and cancelled == [True]
    assert not render._EXPORT_LOCK.locked()


def test_a_timed_out_render_keeps_the_export_lock_until_it_stops(studio, monkeypatch):
    """Cancelling only asks: until the encode really stops, a retry must not write the same file."""
    import threading
    import time
    from classes.editor_tools import project_export_render as render
    from classes.editor_tools._base import ToolError

    def timed_out(func, *a):
        raise ToolError("the render did not finish within 1 s")

    monkeypatch.setattr(render, "run_on_qthread", timed_out)
    stopped = threading.Event()
    sys.modules["windows.export"].cancel_headless_exports = lambda *a, **k: time.sleep(0.01) or stopped.is_set()
    out = studio.call("export_video_tool", file_name="slow")
    assert out.startswith("Error") and "did not finish" in out
    try:
        out = studio.call("export_video_tool", file_name="retry")
        assert out.startswith("Error") and "already running" in out
    finally:
        stopped.set()
    deadline = time.time() + 5
    while render._EXPORT_LOCK.locked() and time.time() < deadline:
        time.sleep(0.01)
    assert not render._EXPORT_LOCK.locked()


def test_export_audio_gif_and_sequence_renders(studio):
    _receipt(studio.call("export_video_tool", preset="MP3", file_name="mix"))
    assert studio.renders[-1]["export_type"] == "Audio Only" and studio.renders[-1]["path"].endswith("mix.mp3")
    _receipt(studio.call("export_video_tool", preset="GIF", width=480, height=270, start=1, end=3))
    assert studio.renders[-1]["export_type"] == "Video Only"
    assert studio.renders[-1]["audio"]["channels"] == 0
    data = _receipt(studio.call("export_video_tool", export_type="image_sequence", file_name="still", end=1.2))
    assert data["frame_files"] == 2
    out = studio.call("export_video_tool", export_type="image_sequence", file_name="still", end=1.2)
    assert out.startswith("Error") and "already exist" in out


def test_export_failures_are_errors(studio, monkeypatch):
    sys.modules["windows.export"].export_video_headless = lambda *a, **k: "Invalid range of frames to export."
    out = studio.call("export_video_tool", file_name="bad")
    assert out.startswith("Error") and "Invalid range" in out
    sys.modules["windows.export"].export_video_headless = lambda *a, **k: None  # writes nothing
    out = studio.call("export_video_tool", file_name="empty")
    assert out.startswith("Error") and "missing or empty" in out


def test_export_refuses_input_files_and_empty_timelines(studio):
    src = studio.file(studio.clip(studio.clip_id)["file_id"])["path"]
    out = studio.call("export_video_tool", output_path=src)
    assert out.startswith("Error") and "input files" in out
    studio.store._data["clips"] = []
    out = studio.call("export_video_tool")
    assert out.startswith("Error") and "timeline is empty" in out


def test_show_dialog_opens_the_export_window_without_rendering(studio):
    data = _receipt(studio.call("export_video_tool", show_dialog=True))
    assert data["dialog_opened"] is True and studio.renders == []
    sys.modules["windows.export"].Export.return_value.show.assert_called_once()


# --- export settings ------------------------------------------------------

def test_export_settings_are_validated_and_outside_undo(studio):
    out = studio.call("set_export_setting_tool", key="colour_depth", value="10")
    assert out.startswith("Error") and "valid keys" in out and "video_bitrate" in out
    assert "even" in studio.call("set_export_setting_tool", key="width", value="1001")
    assert "low, med or high" in studio.call("set_export_setting_tool", key="quality", value="ultra")
    data = _receipt(studio.call("set_export_setting_tool", key="preset", value="tiktok"))
    assert data["stored"] == {"preset": "TikTok"}
    _receipt(studio.call("set_export_setting_tool", key="start", value="2"))
    _receipt(studio.call("set_export_setting_tool", key="vcodec", value="libx264"))
    assert studio.undo_steps_since_mark() == 0
    data = _receipt(studio.call("get_export_settings_tool"))
    assert data["effective"]["preset"] == "TikTok" and data["effective"]["start_seconds"] == 2.0
    assert "preset" in data["valid_keys"]
    _receipt(studio.call("export_video_tool", file_name="stored"))
    assert studio.renders[-1]["video"]["start_frame"] == 61
    data = _receipt(studio.call("set_export_setting_tool", key="start", clear=True))
    assert data["cleared"] == ["start_frame"]
    data = _receipt(studio.call("set_export_setting_tool", clear=True))
    assert set(data["cleared"]) == {"preset", "video_codec"} and data["overrides"] == {}
    assert _receipt(studio.call("get_export_settings_tool"))["effective"]["preset"] == "MP4 (h.264)"


# --- frames and files -----------------------------------------------------

def test_save_frame_image(studio, monkeypatch):
    """Review #216: the full-resolution render ran on the GUI thread (via the
    preview timeline); it now renders a private timeline on a QThread."""
    from classes.editor_tools import effects_color_analysis as eca
    from classes.editor_tools import project_export_render as render

    renders, fmts, closed = [], [], []

    class _Frame:
        def __init__(self, works=True):
            self.works = works

        def Save(self, path, scale, fmt):
            fmts.append(fmt)
            if self.works:
                with open(path, "wb") as fh:
                    fh.write(b"img")

    def fake_render(clip_data, time_s, strip=None):
        renders.append(clip_data)
        return types.SimpleNamespace(frame=_Frame(), close=lambda: closed.append(1))

    monkeypatch.setattr(eca, "render_frame", fake_render)
    monkeypatch.setattr(render, "on_main", MagicMock(side_effect=AssertionError("GUI thread")))
    on_qthread = []
    monkeypatch.setattr(render, "run_on_qthread", lambda func, *a, **k: (on_qthread.append(1), func())[1])

    data = _receipt(studio.call("save_frame_image_tool", time=2.0, file_path=str(studio.out_dir / "still")))
    assert data["path"].endswith("still.png") and data["frame"] == 61 and data["width"] == 1920
    assert renders == [None] and on_qthread and closed  # the composite, on a QThread
    assert "already exists" in studio.call("save_frame_image_tool", time=2.0, file_path=data["path"])
    _receipt(studio.call("save_frame_image_tool", time=3, file_path=str(studio.out_dir / "s.jpg")))
    assert fmts[-1] == "JPG"
    assert "after the end" in studio.call("save_frame_image_tool", time=99)
    monkeypatch.setattr(eca, "render_frame", lambda *a, **k: types.SimpleNamespace(
        frame=_Frame(works=False), close=lambda: None))
    assert studio.call("save_frame_image_tool", time=1, file_path=str(studio.out_dir / "x.png")).startswith("Error")
    assert studio.undo_steps_since_mark() == 0

    # PR #275 review: a render that outlives its timeout must not publish its
    # frame after the tool already reported the failure.
    monkeypatch.setattr(eca, "render_frame", fake_render)
    late = []

    def timed_out(func, *a, **k):
        late.append(func)
        raise render.ToolError("the render did not finish within 120 s")

    monkeypatch.setattr(render, "run_on_qthread", timed_out)
    target = studio.out_dir / "late.png"
    assert studio.call("save_frame_image_tool", time=1, file_path=str(target)).startswith("Error")
    assert late[0]() is False
    assert not target.exists()
    assert [n for n in os.listdir(str(studio.out_dir)) if "late" in n] == []

    # ...and one that had already published when the wait gave up is a success,
    # not an error the caller would retry over a file that is there.
    def published_then_timed_out(func, *a, **k):
        func()
        raise render.ToolError("the render did not finish within 120 s")

    monkeypatch.setattr(render, "run_on_qthread", published_then_timed_out)
    data = _receipt(studio.call("save_frame_image_tool", time=1, file_path=str(studio.out_dir / "slow.png")))
    assert os.path.isfile(data["path"])


def test_export_files_to_folder(studio, tmp_path, monkeypatch):
    src = tmp_path / "shot.mp4"
    src.write_bytes(b"data")
    fid = studio.add_file("video", path=str(src))
    fake = types.ModuleType("windows.export_clips")
    fake.isClip = lambda f: False
    fake.isImageSequence = lambda f: False
    fake.nameOfExport = lambda f: "shot [0.00 - 1.00].mp4"
    fake.copyFileToFolder = lambda f, dest: open(os.path.join(dest, "shot [0.00 - 1.00].mp4"), "wb").write(b"d")
    monkeypatch.setitem(sys.modules, "windows.export_clips", fake)
    import windows
    monkeypatch.setattr(windows, "export_clips", fake, raising=False)
    monkeypatch.setitem(sys.modules, "openshot", types.ModuleType("openshot"))
    dest = tmp_path / "handoff"
    data = _receipt(studio.call("export_files_to_folder_tool", file_ids=[fid], folder=str(dest)))
    assert data["files"][0]["status"] == "copied" and os.path.isfile(data["files"][0]["path"])
    data = _receipt(studio.call("export_files_to_folder_tool", file_ids=[fid], folder=str(dest)))
    assert data["files"][0]["status"] == "skipped (exists)"
    assert "no project file" in studio.call("export_files_to_folder_tool", file_ids=["nope"])


def test_subclip_renders_run_on_a_qthread_not_the_worker(studio, tmp_path, monkeypatch):
    """Rendering on a plain threading.Thread can deadlock Qt's font cache against the GIL."""
    from classes.editor_tools import project_export_render as render

    src = tmp_path / "long.mp4"
    src.write_bytes(b"data")
    fid = studio.add_file("video", path=str(src), start=1.0, end=2.0)
    fake = types.ModuleType("windows.export_clips")
    fake.isClip = lambda f: True
    fake.isImageSequence = lambda f: False
    fake.nameOfExport = lambda f: "long [1.00 - 2.00].mp4"
    fake.startAndEndFrames = lambda c: (31, 60)
    fake.setupWriter = lambda c, w: None
    monkeypatch.setitem(sys.modules, "windows.export_clips", fake)
    import windows  # an earlier test may have imported the real module as a package attribute
    monkeypatch.setattr(windows, "export_clips", fake, raising=False)
    writes = []
    fake_os = types.ModuleType("openshot")
    fake_os.FFmpegWriter = lambda path: MagicMock(WriteFrame=lambda fr: writes.append(fr),
                                                   Close=lambda: open(path, "wb").write(b"x"))
    fake_os.Clip = lambda path: MagicMock(GetFrame=lambda n: n)
    monkeypatch.setitem(sys.modules, "openshot", fake_os)
    seen = []
    monkeypatch.setattr(render, "run_on_qthread", lambda func, *a: (seen.append(func), func())[1])
    data = _receipt(studio.call("export_files_to_folder_tool", file_ids=[fid], folder=str(tmp_path / "out")))
    assert data["files"][0]["status"] == "rendered" and len(seen) == 1 and writes == list(range(31, 61))


def test_a_second_export_while_one_renders_does_nothing(editor):
    """A retry after a timed-out call must not queue a second render over the same file."""
    from classes.editor_tools import project_export_render as per

    assert per._EXPORT_LOCK.acquire(blocking=False)
    per._RUNNING_EXPORT.update(path="/tmp/out.mp4", started="12:00:00")
    try:
        out = editor.call("export_video_tool", preset="MP4 (h.264)", output_path="/tmp/other.mp4")
    finally:
        per._RUNNING_EXPORT.clear()
        per._EXPORT_LOCK.release()
    assert out.startswith("Error") and "already running" in out and "did nothing" in out, out
    assert editor.undo_steps_since_mark() == 0


def test_a_timed_out_render_thread_is_stopped_and_kept_alive(monkeypatch):
    """A QThread destroyed while it still runs aborts the app, and a render left
    writing would overlap the next file's."""
    from classes.editor_tools import project_export_render as per
    from classes.editor_tools._base import ToolError

    made = []

    class StuckThread:
        def __init__(self):
            self.interrupted = False
            self.finished = MagicMock()
            made.append(self)

        def start(self):
            pass

        def wait(self, _ms):
            return False

        def requestInterruption(self):
            self.interrupted = True

    monkeypatch.setattr(per, "th", lambda: types.SimpleNamespace(QThread=StuckThread))
    monkeypatch.setattr(per, "_ABANDONED_JOBS", [], raising=False)
    with pytest.raises(ToolError, match="did not finish"):
        per.run_on_qthread(lambda: None, timeout_seconds=0.01)
    assert made[0].interrupted
    assert per._ABANDONED_JOBS == [made[0]]


def test_a_failed_subclip_render_leaves_no_file_to_skip_later(studio, tmp_path, monkeypatch):
    """Review #216: the file was removed before the writer closed, and Close
    wrote it back, so the next export skipped a broken file as existing."""
    from classes.editor_tools import project_export_render as render

    src = tmp_path / "long.mp4"
    src.write_bytes(b"data")
    fid = studio.add_file("video", path=str(src), start=1.0, end=2.0)
    fake = types.ModuleType("windows.export_clips")
    fake.isClip = lambda f: True
    fake.isImageSequence = lambda f: False
    fake.nameOfExport = lambda f: "long [1.00 - 2.00].mp4"
    fake.startAndEndFrames = lambda c: (31, 60)
    fake.setupWriter = lambda c, w: None
    monkeypatch.setitem(sys.modules, "windows.export_clips", fake)
    import windows
    monkeypatch.setattr(windows, "export_clips", fake, raising=False)

    def broken_write(fr):
        raise RuntimeError("encoder died")

    fake_os = types.ModuleType("openshot")
    fake_os.FFmpegWriter = lambda path: MagicMock(WriteFrame=broken_write,
                                                   Close=lambda: open(path, "wb").write(b"partial"))
    fake_os.Clip = lambda path: MagicMock(GetFrame=lambda n: n)
    monkeypatch.setitem(sys.modules, "openshot", fake_os)
    monkeypatch.setattr(render, "run_on_qthread", lambda func, *a: func())
    out_dir = tmp_path / "out"
    out = studio.call("export_files_to_folder_tool", file_ids=[fid], folder=str(out_dir))
    assert "encoder died" in out
    assert os.listdir(out_dir) == []
