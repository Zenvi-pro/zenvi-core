"""Headless unit tests for the Optimize Preview proxy service (classes/proxy_service.py).

Ported from upstream OpenShot ``src/tests/test_proxy_service.py`` (PR #5993) onto
Zenvi's stubbed-Qt pytest suite: no QApplication, ``get_app`` is patched, and the
libopenshot calls are replaced with fakes.  The real-libopenshot transcode path is
covered by ``test_proxy_service_real.py`` (``ZENVI_REAL_QT=1``).
"""

import json
import os
import threading
import types
from unittest.mock import Mock, patch

import pytest

from classes import proxy_service
from classes.proxy_service import ProxyService, dialog_preview_reader_data


class _Settings:
    def __init__(self):
        self.values = {"default-profile": "HD 720p 30 fps"}

    def get(self, key):
        return self.values.get(key)


class _Signal:
    def __init__(self):
        self.calls = 0

    def emit(self, *args):
        self.calls += 1


class _Timeline:
    def __init__(self):
        self.payloads = []
        self.cache_clears = []

    def ApplyJsonDiff(self, payload):
        self.payloads.append(payload)

    def ClearAllCache(self, clear_images):
        self.cache_clears.append(bool(clear_images))


class _Window:
    def __init__(self):
        self.timeline_sync = types.SimpleNamespace(timeline=_Timeline())
        self.refreshFrameSignal = _Signal()
        self.status_messages = []
        self.statusBar = types.SimpleNamespace(
            showMessage=lambda text, ms: self.status_messages.append((text, ms))
        )


class _Service(ProxyService):
    """ProxyService without the QObject parent call (the stub QObject is ``object``)."""

    def __init__(self, win):
        self.win = win
        self._executor = None
        self._jobs = {}
        self._lock = threading.RLock()
        self._ensure_executor()
        self.proxy_generated.connect(self._on_proxy_generated)


@pytest.fixture
def app():
    fake = types.SimpleNamespace(
        settings=_Settings(),
        project=None,
        updates=types.SimpleNamespace(transaction_id=None, delete=lambda key: None),
        window=None,
    )
    fake.get_settings = lambda: fake.settings
    fake._tr = lambda text: text
    with patch.object(proxy_service, "get_app", return_value=fake):
        yield fake


@pytest.fixture
def service(app):
    win = _Window()
    app.window = win
    svc = _Service(win)
    yield svc
    svc.shutdown()


def _file(file_id, **data):
    data.setdefault("id", file_id)
    return types.SimpleNamespace(id=file_id, data=data)


# --- dialog_preview_reader_data -------------------------------------------------

def test_dialog_preview_reader_data_prefers_valid_proxy_reader():
    file_obj = _file("F1", path="/media/source.mp4", proxy_reader={"path": "/optimized/F1.mp4", "width": 640})
    with patch.object(proxy_service, "absolute_media_path", side_effect=lambda p: p), \
         patch.object(proxy_service.os.path, "exists", side_effect=lambda p: p == "/optimized/F1.mp4"):
        reader = dialog_preview_reader_data(file_obj)
    assert reader["path"] == "/optimized/F1.mp4"
    assert reader["width"] == 640
    assert reader["id"] == "F1"


def test_dialog_preview_reader_data_falls_back_when_proxy_missing():
    file_obj = _file("F1", path="/media/source.mp4", width=1920,
                     proxy_reader={"path": "/optimized/F1.mp4", "width": 640})
    with patch.object(proxy_service, "absolute_media_path", side_effect=lambda p: p), \
         patch.object(proxy_service.os.path, "exists", return_value=False):
        reader = dialog_preview_reader_data(file_obj)
    assert reader["path"] == "/media/source.mp4"
    assert reader["width"] == 1920


def test_dialog_preview_reader_data_ignores_missing_marker_even_if_file_exists():
    file_obj = _file("F1", path="/media/source.mp4", width=1920,
                     proxy_reader={"path": "/optimized/F1.mp4", "width": 640, "missing": True})
    with patch.object(proxy_service, "absolute_media_path", side_effect=lambda p: p), \
         patch.object(proxy_service.os.path, "exists", return_value=True):
        reader = dialog_preview_reader_data(file_obj)
    assert reader["path"] == "/media/source.mp4"


def test_dialog_preview_reader_data_can_force_source(app):
    file_obj = _file("F1", path="/media/source.mp4", width=1920,
                     proxy_reader={"path": "/optimized/F1.mp4", "width": 640})
    with patch.object(proxy_service, "absolute_media_path", side_effect=lambda p: p), \
         patch.object(proxy_service.os.path, "exists", return_value=True):
        reader = dialog_preview_reader_data(file_obj, prefer_proxy=False)
    assert reader["path"] == "/media/source.mp4"


# --- JSON rewrite --------------------------------------------------------------

def _payload_with_proxy(proxy_path="/cache/F1.mp4"):
    return {
        "files": [{"id": "F1", "path": "/media/source.mp4",
                   "proxy_reader": {"id": "F1", "path": proxy_path, "width": 1280}}],
        "clips": [{"id": "C1", "file_id": "F1",
                   "reader": {"id": "F1", "path": "/media/source.mp4", "width": 3840},
                   "effects": [{"id": "CE1",
                                "mask_reader": {"id": "F1", "path": "/media/source.mp4", "width": 3840}}]}],
    }


def test_rewrite_json_for_preview_replaces_clip_and_effect_readers_only(service):
    with patch.object(proxy_service.os.path, "exists", return_value=True):
        rewritten = service.rewrite_json_for_preview(_payload_with_proxy())
    assert rewritten["files"][0]["path"] == "/media/source.mp4"
    assert rewritten["clips"][0]["reader"]["path"] == "/cache/F1.mp4"
    assert rewritten["clips"][0]["effects"][0]["mask_reader"]["path"] == "/cache/F1.mp4"


def test_rewrite_json_for_preview_accepts_text_payload(service):
    with patch.object(proxy_service.os.path, "exists", return_value=True):
        rewritten = service.rewrite_json_for_preview(json.dumps(_payload_with_proxy()))
    assert isinstance(rewritten, str)
    assert json.loads(rewritten)["clips"][0]["reader"]["path"] == "/cache/F1.mp4"


def test_rewrite_json_for_preview_is_noop_without_proxy_readers(service):
    payload = json.dumps({"clips": [{"id": "C1", "reader": {"id": "F1", "path": "/media/source.mp4"}}]})
    assert service.rewrite_json_for_preview(payload) == payload


def test_rewrite_json_for_preview_ignores_missing_proxy_file(service):
    payload = _payload_with_proxy("/missing/F1.mp4")
    with patch.object(proxy_service.os.path, "exists", return_value=False):
        assert service.rewrite_json_for_preview(payload) == payload


def test_rewrite_json_for_preview_uses_project_files_for_diffs(service, app):
    """A clip-only diff (no ``files`` list) still resolves proxies from the open project."""
    app.project = types.SimpleNamespace(get=lambda key: _payload_with_proxy()["files"] if key == "files" else None)
    diff = [{"type": "insert", "key": ["clips"], "value": {"id": "C1", "reader": {"id": "F1", "path": "/media/source.mp4"}}}]
    with patch.object(proxy_service.os.path, "exists", return_value=True):
        rewritten = service.rewrite_json_for_preview(json.dumps(diff))
    assert json.loads(rewritten)[0]["value"]["reader"]["path"] == "/cache/F1.mp4"


# --- runtime updates -----------------------------------------------------------

def test_apply_runtime_updates_for_file_targets_related_clips_and_effects(service):
    file_obj = _file("F1", path="/media/source.mp4")
    clip_obj = types.SimpleNamespace(id="C1", data={"id": "C1", "file_id": "F1",
                                                    "reader": {"id": "F1", "path": "/media/source.mp4"}})
    transition_obj = types.SimpleNamespace(id="T1", data={"id": "T1", "reader": {"id": "F1", "path": "/media/source.mp4"}})
    with patch.object(proxy_service.File, "get", return_value=file_obj), \
         patch.object(proxy_service.Clip, "filter", return_value=[clip_obj]), \
         patch.object(proxy_service.Transition, "filter", return_value=[transition_obj]), \
         patch.object(service, "rewrite_json_for_preview", side_effect=lambda payload: payload):
        assert service.apply_runtime_updates_for_file("F1")
    timeline = service.win.timeline_sync.timeline
    assert len(timeline.payloads) == 1
    assert len(json.loads(timeline.payloads[0])) == 2
    assert timeline.cache_clears == [True]
    assert service.win.refreshFrameSignal.calls == 1


def test_apply_runtime_updates_for_files_batches_multiple_file_ids(service):
    clip_one = types.SimpleNamespace(id="C1", data={"id": "C1", "file_id": "F1", "reader": {"id": "F1", "path": "/a.mp4"}})
    clip_two = types.SimpleNamespace(id="C2", data={"id": "C2", "file_id": "F2", "reader": {"id": "F2", "path": "/b.mp4"}})
    with patch.object(proxy_service.Clip, "filter", return_value=[clip_one, clip_two]), \
         patch.object(proxy_service.Transition, "filter", return_value=[]), \
         patch.object(service, "_payload_references_file", side_effect=lambda payload, fid: payload.get("file_id") == fid), \
         patch.object(service, "rewrite_json_for_preview", side_effect=lambda payload: payload):
        assert service.apply_runtime_updates_for_files(["F1", "F2"])
    timeline = service.win.timeline_sync.timeline
    assert len(timeline.payloads) == 1
    assert len(json.loads(timeline.payloads[0])) == 2


def test_apply_runtime_updates_returns_false_without_timeline(service):
    service.win.timeline_sync = None
    assert service.apply_runtime_updates_for_file("F1") is False


# --- transcode driver (fakes for libopenshot) ----------------------------------

def test_build_proxy_reader_opens_and_closes_source_clip(service):
    class Cache:
        def __init__(self):
            self.max_bytes = []
            self.clears = 0

        def SetMaxBytes(self, value):
            self.max_bytes.append(int(value))

        def Clear(self):
            self.clears += 1

    clip_cache, reader_cache = Cache(), Cache()
    clip_reader = types.SimpleNamespace(
        Json=lambda: json.dumps({"width": 1920, "height": 1080, "fps": {"num": 30, "den": 1},
                                 "pixel_ratio": {"num": 1, "den": 1}, "video_length": 3, "has_audio": False}),
        GetFrame=lambda frame: "frame-{}".format(frame),
        GetCache=lambda: reader_cache,
        info=types.SimpleNamespace(metadata=types.SimpleNamespace(count=lambda key: 0)),
    )
    clip_obj = types.SimpleNamespace(opened=False, closed=False, parent_timeline_calls=[])
    clip_obj.Open = lambda: setattr(clip_obj, "opened", True)
    clip_obj.Close = lambda: setattr(clip_obj, "closed", True)
    clip_obj.Reader = lambda: clip_reader
    clip_obj.GetCache = lambda: clip_cache
    clip_obj.ParentTimeline = lambda timeline: clip_obj.parent_timeline_calls.append(timeline)
    created_timelines = []

    def fake_timeline(width, height, fps, sample_rate, channels, layout):
        timeline = types.SimpleNamespace(width=width, height=height, preview_width=width, preview_height=height)
        created_timelines.append(timeline)
        return timeline

    writer = types.SimpleNamespace(opened=False, closed=False, frames=[], video_options=[])
    writer.SetVideoOptions = lambda *args: writer.video_options.append(args)
    writer.PrepareStreams = lambda: None
    writer.SetAudioOptions = lambda *args: None
    writer.Open = lambda: setattr(writer, "opened", True)
    writer.Close = lambda: setattr(writer, "closed", True)
    writer.WriteFrame = lambda frame: writer.frames.append(frame)
    thumbs = []

    with patch.object(proxy_service, "absolute_media_path", return_value="/media/source.mp4"), \
         patch.object(proxy_service.os.path, "exists", side_effect=lambda p: p == "/media/source.mp4"), \
         patch.object(proxy_service.os, "listdir", return_value=[]), \
         patch.object(proxy_service.os, "makedirs"), \
         patch.object(proxy_service.openshot, "Clip", create=True, return_value=clip_obj), \
         patch.object(proxy_service.openshot, "Timeline", create=True, side_effect=fake_timeline), \
         patch.object(proxy_service.openshot, "FFmpegWriter", create=True, return_value=writer), \
         patch.object(proxy_service.openshot, "Fraction", create=True, side_effect=lambda num, den: (num, den)), \
         patch.object(proxy_service.openshot, "LAYOUT_STEREO", 3, create=True), \
         patch.object(proxy_service.openshot, "LAYOUT_MONO", 4, create=True), \
         patch.object(proxy_service, "GenerateThumbnailFromFrame",
                      side_effect=lambda frame, path, w, h, mask, overlay, rotate=0.0: thumbs.append((frame, path))), \
         patch.object(service, "_proxy_root", return_value="/tmp/proxies"), \
         patch.object(service, "_reader_json_for_path", return_value={"id": "F1", "path": "/tmp/proxies/source_proxy.mp4"}):
        result = service._build_proxy_reader("F1", {"path": "/media/source.mp4", "media_type": "video", "fingerprint": None})

    assert clip_obj.opened and clip_obj.closed
    assert writer.opened and writer.closed
    assert created_timelines[0].preview_width == 1280 and created_timelines[0].preview_height == 720
    assert clip_obj.parent_timeline_calls[0] is created_timelines[0]
    assert clip_obj.parent_timeline_calls[-1] is None
    assert writer.frames == ["frame-1", "frame-2", "frame-3"]
    # 1920x1080 scaled into the 720p default bound
    assert writer.video_options[0][3:5] == (1280, 720)
    assert [os.path.basename(path) for _frame, path in thumbs] == ["1.png", "3.png"]
    assert clip_cache.max_bytes == [service.OPTIMIZE_CACHE_MAX_BYTES]
    assert reader_cache.max_bytes == [service.OPTIMIZE_CACHE_MAX_BYTES]
    assert result["path"] == "/tmp/proxies/source_proxy.mp4"


def test_build_proxy_reader_rejects_non_video(service):
    with patch.object(proxy_service, "absolute_media_path", return_value="/media/song.mp3"), \
         patch.object(proxy_service.os.path, "exists", return_value=True):
        with pytest.raises(RuntimeError):
            service._build_proxy_reader("F1", {"path": "/media/song.mp3", "media_type": "audio"})


def test_scaled_dimensions_keep_aspect_and_even_sizes():
    assert ProxyService._scaled_dimensions(3840, 2160) == (1280, 720)
    assert ProxyService._scaled_dimensions(1080, 1920, 1280, 720) == (404, 720)
    assert ProxyService._scaled_dimensions(640, 360) == (640, 360)  # never upscale


def test_thumbnail_prewarm_frames_uses_coarse_4fps_grid(service):
    with patch.object(service, "_thumbnail_prewarm_rate", return_value=4):
        assert service._thumbnail_prewarm_frames("F1", 31, {"num": 30, "den": 1}) == [1, 9, 17, 25, 31]


# --- settings ------------------------------------------------------------------

def test_executor_defaults_to_single_worker(service):
    assert service._executor._max_workers == 1


def test_optimize_settings_override_workers_size_and_thumbnail_rate(service, app):
    app.settings.values.update({"optimize-preview-jobs": 3, "optimize-preview-max-size": "1920x1080",
                                "optimize-preview-thumbnails": 5})
    service._jobs.clear()
    service._ensure_executor()
    assert service._executor._max_workers == 3
    assert service._max_optimize_bounds() == (1920, 1080)
    assert service._thumbnail_prewarm_rate() == 5


def test_invalid_size_setting_falls_back_to_720p(service, app):
    app.settings.values["optimize-preview-max-size"] = "garbage"
    assert service._max_optimize_bounds() == (1280, 720)


# --- create / link / remove ------------------------------------------------------

def test_create_for_files_skips_already_optimized_files(service):
    ready_file = _file("F1", proxy_reader={"path": "/optimized/F1.mp4"})
    new_file = _file("F2", path="/media/source.mp4", media_type="video")
    submitted = []
    with patch.object(service, "has_missing_proxy", return_value=False), \
         patch.object(service, "_proxy_root", return_value="/tmp/optimized"), \
         patch.object(proxy_service.os, "makedirs"), \
         patch.object(proxy_service.os, "listdir", return_value=[]), \
         patch.object(service._executor, "submit",
                      side_effect=lambda *args: submitted.append(args) or Mock(add_done_callback=lambda cb: None)):
        service.create_for_files([ready_file, new_file])
    assert len(submitted) == 1 and submitted[0][1] == "F2"
    assert service.win.status_messages[-1][0] == "Optimize Preview: creating 1 item(s), skipped 1"
    assert service.get_file_badge("F2")["status"] == "queued"


def test_create_for_files_links_existing_target_file_instead_of_rerendering(service):
    file_obj = _file("F1", path="/media/source.mp4", media_type="video")
    submitted = []
    with patch.object(service, "has_missing_proxy", return_value=False), \
         patch.object(proxy_service.os, "makedirs"), \
         patch.object(service, "_existing_proxy_output_path", return_value="/tmp/optimized/source_proxy.mp4"), \
         patch.object(service, "_reader_json_for_path", return_value={"id": "F1", "path": "/tmp/optimized/source_proxy.mp4"}), \
         patch.object(service, "_save_proxy_reader") as save_proxy_reader, \
         patch.object(service._executor, "submit", side_effect=lambda *args: submitted.append(args)):
        service.create_for_files([file_obj])
    save_proxy_reader.assert_called_once_with("F1", {"id": "F1", "path": "/tmp/optimized/source_proxy.mp4"})
    assert submitted == []
    assert service.win.status_messages[-1][0] == "Optimize Preview: linked 1 item(s)"


def test_existing_proxy_output_path_reuses_default_name_when_file_exists(service):
    with patch.object(service, "_proxy_root", return_value="/project/optimized"), \
         patch.object(proxy_service.os.path, "exists", side_effect=lambda p: p == "/project/optimized/clip001_proxy.mp4"):
        assert service._existing_proxy_output_path("F2", {"path": "/media/clip001.mov"}) == "/project/optimized/clip001_proxy.mp4"


def test_get_proxy_state_returns_ready_and_missing(service):
    file_obj = _file("F1", proxy_reader={"path": "/tmp/proxies/F1.mp4"})
    with patch.object(proxy_service.os.path, "exists", return_value=True):
        assert service.get_proxy_state(file_obj) == "ready"
    with patch.object(proxy_service.os.path, "exists", return_value=False):
        assert service.get_proxy_state(file_obj) == "missing"
    marked = _file("F2", proxy_reader={"path": "/tmp/proxies/F2.mp4", "missing": True})
    with patch.object(proxy_service.os.path, "exists", return_value=True):
        assert service.get_proxy_state(marked) == "missing"
    assert service.get_proxy_state(_file("F3", path="/x.mp4")) == "none"


def test_use_existing_for_files_links_matches_and_marks_missing(service):
    file_one = _file("F1", path="/media/source-a.mp4")
    file_two = _file("F2", path="/media/source-b.mp4")
    saved = []
    chooser = types.SimpleNamespace(getExistingDirectory=Mock(return_value="/optimized"))
    with patch.object(service, "_proxy_root", return_value="/project_assets/optimized"), \
         patch.object(proxy_service, "QFileDialog", chooser), \
         patch.object(service, "_index_existing_optimized_files", return_value={
             "basename": {"source-a.mp4": ["/optimized/F1.mp4"]}, "stem": {"f1": ["/optimized/F1.mp4"]}, "path": {}}), \
         patch.object(proxy_service.os.path, "exists", side_effect=lambda p: p == "/optimized/F1.mp4"), \
         patch.object(service, "_reader_json_for_path", return_value={"id": "F1", "path": "/optimized/F1.mp4"}), \
         patch.object(service, "_save_proxy_reader", side_effect=lambda fid, reader, **kw: saved.append((fid, reader))), \
         patch.object(service, "apply_runtime_updates_for_files", return_value=True), \
         patch.object(service, "_emit_job_change"):
        service.use_existing_for_files([file_one, file_two])
    assert chooser.getExistingDirectory.call_args[0][2] == "/project_assets/optimized"
    assert saved[0] == ("F1", {"id": "F1", "path": "/optimized/F1.mp4"})
    assert saved[1][0] == "F2" and saved[1][1]["missing"]
    assert saved[1][1]["path"] == "/optimized/source-b_proxy.mp4"


def test_use_existing_for_files_cancelled_dialog_changes_nothing(service):
    chooser = types.SimpleNamespace(getExistingDirectory=Mock(return_value=""))
    with patch.object(proxy_service, "QFileDialog", chooser), \
         patch.object(service, "_save_proxy_reader") as save_proxy_reader:
        service.use_existing_for_files([_file("F1", path="/media/a.mp4")])
    save_proxy_reader.assert_not_called()


@pytest.mark.parametrize("stems, expected", [
    ({"clip001": ["/optimized/clip001.mkv"]}, "/optimized/clip001.mkv"),
    ({"clip001_proxy": ["/optimized/clip001_proxy.mp4"]}, "/optimized/clip001_proxy.mp4"),
    ({"clip001_reviewcopy": ["/optimized/clip001_reviewcopy.mp4"]}, "/optimized/clip001_proxy.mp4"),
    ({"clip001_proxy_f1": ["/optimized/clip001_proxy_F1.mp4"]}, "/optimized/clip001_proxy_F1.mp4"),
    ({"f1": ["/optimized/F1.mp4"]}, "/optimized/F1.mp4"),
])
def test_match_existing_optimized_path(service, stems, expected):
    file_obj = _file("F1", path="/media/clip001.mov")
    normalized = {"clip001": stems["clip001_proxy"]} if "clip001_proxy" in stems else {}
    index = {"basename": {}, "stem": stems, "normalized": normalized, "path": {}}
    assert service._match_existing_optimized_path(file_obj, "/optimized", index) == expected


def test_preferred_proxy_filename_uses_source_name_and_disambiguates(service):
    assert service._preferred_proxy_filename("F1", {"path": "/media/clip001.mov"}, "/p", existing_names={"other_proxy.mp4"}) == "clip001_proxy.mp4"
    assert service._preferred_proxy_filename("F1", {"path": "/media/clip001.mov"}, "/p", existing_names={"clip001_proxy.mp4"}) == "clip001_proxy_F1.mp4"


def test_reserve_proxy_output_path_avoids_collisions_with_reserved_jobs(service):
    service._jobs["F1"] = {"id": "F1", "status": "queued", "progress": 0, "cancel_requested": False,
                           "output_path": "/project/optimized/clip001_proxy.mp4"}
    with patch.object(service, "_proxy_root", return_value="/project/optimized"), \
         patch.object(proxy_service.os, "listdir", return_value=[]), \
         patch.object(proxy_service.os, "makedirs"):
        assert service._reserve_proxy_output_path("F2", {"path": "/media/clip001.mov"}) == "/project/optimized/clip001_proxy_F2.mp4"


def test_index_existing_optimized_files_limits_to_video_extensions(service):
    def fake_walk(_):
        yield ("/optimized", [], ["a_proxy.mp4", "a_proxy.thm", "a_proxy.txt", "a_proxy.mxf"])
    with patch.object(proxy_service.os, "walk", side_effect=fake_walk):
        index = service._index_existing_optimized_files("/optimized")
    assert set(index["basename"]) == {"a_proxy.mp4", "a_proxy.mxf"}


def test_delete_and_unlink_for_files_deletes_linked_proxy_and_unlinks_all(service, app):
    fresh = [
        types.SimpleNamespace(id="F1", key=["files", {"id": "F1"}], data={"id": "F1", "proxy_reader": {"path": "/project/optimized/F1.mp4"}}, save=Mock()),
        types.SimpleNamespace(id="F1", key=["files", {"id": "F1"}], data={"id": "F1", "proxy_reader": {"path": "/project/optimized/F1.mp4"}}),
        types.SimpleNamespace(id="F2", key=["files", {"id": "F2"}], data={"id": "F2", "proxy_reader": {"path": "/external/F2.mp4"}}, save=Mock()),
        types.SimpleNamespace(id="F2", key=["files", {"id": "F2"}], data={"id": "F2", "proxy_reader": {"path": "/external/F2.mp4"}}),
    ]
    delete_calls, removed = [], []
    app.updates = types.SimpleNamespace(delete=lambda key: delete_calls.append(key), transaction_id=None)
    with patch.object(proxy_service.File, "get", side_effect=fresh), \
         patch.object(proxy_service, "absolute_media_path", side_effect=lambda p: p), \
         patch.object(proxy_service.os.path, "exists", return_value=True), \
         patch.object(proxy_service.os, "remove", side_effect=lambda p: removed.append(p)), \
         patch.object(service, "apply_runtime_updates_for_files", return_value=True), \
         patch.object(service, "_emit_job_change"):
        deleted = service.delete_and_unlink_for_files([_file("F1"), _file("F2")])
    assert deleted == 2
    assert removed == ["/project/optimized/F1.mp4", "/external/F2.mp4"]
    fresh[0].save.assert_called_once_with()
    fresh[2].save.assert_called_once_with()
    assert delete_calls == [["files", {"id": "F1"}, "proxy_reader"], ["files", {"id": "F2"}, "proxy_reader"]]
    assert service.win.status_messages[-1][0] == "Optimize Preview: deleted 2, unlinked 2"


def test_remove_for_files_clears_proxy_reader_then_nested_delete(service, app):
    fresh = types.SimpleNamespace(id="F1", key=["files", {"id": "F1"}], data={"id": "F1", "proxy_reader": {"path": "/o/F1.mp4"}}, save=Mock())
    refreshed = types.SimpleNamespace(id="F1", key=["files", {"id": "F1"}], data={"id": "F1", "proxy_reader": {"path": "/o/F1.mp4"}})
    delete_calls = []
    app.updates = types.SimpleNamespace(delete=lambda key: delete_calls.append(key), transaction_id=None)
    with patch.object(proxy_service.File, "get", side_effect=[fresh, refreshed]), \
         patch.object(service, "apply_runtime_updates_for_files", return_value=True), \
         patch.object(service, "_emit_job_change"):
        service.remove_for_files([_file("F1")])
    fresh.save.assert_called_once_with()
    assert "proxy_reader" not in fresh.data
    assert delete_calls == [["files", {"id": "F1"}, "proxy_reader"]]


# --- job bookkeeping -------------------------------------------------------------

def test_cancel_job_finalizes_queued_future(service):
    future = Mock()
    future.cancel.return_value = True
    service._jobs["F1"] = {"id": "F1", "status": "queued", "progress": 0, "future": future, "cancel_requested": False}
    assert service.cancel_job("F1")
    assert service.get_active_job_for_file("F1") is None
    assert service.get_file_badge("F1") is None


def test_cancel_job_marks_running_job_canceling(service):
    future = Mock()
    future.cancel.return_value = False
    service._jobs["F1"] = {"id": "F1", "status": "running", "progress": 37, "future": future, "cancel_requested": False}
    assert service.cancel_job("F1")
    assert service._jobs["F1"]["cancel_requested"]
    badge = service.get_file_badge("F1")
    assert badge["status"] == "canceling" and badge["progress"] == 37
    with pytest.raises(proxy_service._ProxyJobCanceled):
        service._raise_if_canceled("F1")


def test_get_file_badge_labels(service):
    service._jobs["F1"] = {"id": "F1", "status": "running", "progress": 42, "cancel_requested": False}
    assert service.get_file_badge("F1")["label"] == "Creating 42%"
    service._jobs["F1"]["status"] = "queued"
    assert service.get_file_badge("F1")["label"] == "Queued"
    assert service.get_file_badge("nope") is None


def test_get_active_job_for_file_snapshot_excludes_future(service):
    service._jobs["F1"] = {"id": "F1", "status": "queued", "progress": 12, "future": Mock(), "cancel_requested": False}
    assert service.get_active_job_for_file("F1") == {"id": "F1", "status": "queued", "progress": 12, "cancel_requested": False}


def test_proxy_root_ignores_backup_project_path(service, app):
    app.project = types.SimpleNamespace(current_filepath="/home/test/.openshot_qt/backup.zvn")
    with patch.object(proxy_service.info, "BACKUP_FILE", "/home/test/.openshot_qt/backup.zvn"), \
         patch.object(proxy_service.info, "RECOVERY_PATH", "/home/test/.openshot_qt/recovery"), \
         patch.object(proxy_service.info, "PROXY_PATH", "/home/test/.openshot_qt/optimized"):
        assert service._proxy_root() == "/home/test/.openshot_qt/optimized"


def test_proxy_root_uses_project_assets_folder(service, app, tmp_path):
    project_file = tmp_path / "movie.zvn"
    project_file.write_text("{}")
    app.project = types.SimpleNamespace(current_filepath=str(project_file))
    root = service._proxy_root()
    assert root.endswith(os.path.join("movie_assets", "optimized"))
