"""Unit tests for HyperFrames fetch metadata stamping (no Qt app)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

from classes.tool_handlers import (  # noqa: E402
    _stamp_motion_graphics_file_metadata,
)
import classes.tool_handlers as th  # noqa: E402


def test_import_generated_video_calls_add_files_with_skip_indexing():
    """MG/AI import must not enqueue Gemini indexing."""
    fake_file = SimpleNamespace(id="F1", data={}, absolute_path=lambda: "/tmp/out.mp4")
    fake_query = MagicMock()
    fake_query.File.get.return_value = fake_file
    fake_query.File.filter.return_value = []
    sys.modules["classes.query"] = fake_query

    with patch.object(th, "_output_path_for_generated_video", return_value="/tmp/out.mp4"), patch.object(
        th, "_canonical_media_path", side_effect=lambda p: p
    ), patch.object(th, "_reencode_for_openshot", return_value=("/tmp/out.mp4", None)), patch.object(
        th, "_run_on_main_thread", side_effect=lambda fn, timeout=30: fn()
    ), patch.object(th, "_get_app") as app, patch.object(
        th, "_normalize_imported_file_path"
    ), patch.object(th, "_refresh_imported_file_thumbnail"):
        files_model = MagicMock()
        app.return_value.window.files_model = files_model
        f, err = th._import_generated_video("/tmp/src.mp4")
    assert err is None
    assert f is not None
    files_model.add_files.assert_called()
    kwargs = files_model.add_files.call_args.kwargs
    assert kwargs.get("skip_indexing") is True


def test_import_generated_video_alpha_fail_closed_no_yuv420p():
    """Transparent MG must not silently fall back to opaque yuv420p."""
    with patch.object(th, "_output_path_for_generated_video", return_value="/tmp/out.webm"), patch.object(
        th, "_canonical_media_path", side_effect=lambda p: p
    ), patch.object(th, "_looks_like_alpha_video", return_value=True), patch.object(
        th, "_ffprobe_has_alpha", return_value=False
    ), patch.object(
        th, "_reencode_alpha_for_openshot", return_value=(None, "vp9 alpha failed")
    ), patch.object(th, "_reencode_for_openshot") as opaque:
        f, err = th._import_generated_video("/tmp/src.webm", preserve_alpha=True)
    assert f is None
    assert err is not None
    assert "no opaque fallback" in err.lower() or "alpha import failed" in err.lower()
    opaque.assert_not_called()


def test_stamp_motion_graphics_file_metadata():
    f = SimpleNamespace(data={}, id="FILE1", save=MagicMock())
    with patch("classes.tool_handlers._get_app") as app:
        win = MagicMock()
        app.return_value.window = win
        _stamp_motion_graphics_file_metadata(
            f, label="HyperFrames lt-clean-bar: Alex — Host (transparent overlay)", transparent=True
        )
    assert "motion_graphics" in f.data["tags"]
    assert "transparent_overlay" in f.data["tags"]
    assert f.data["ai_metadata"]["short_summary"].startswith("HyperFrames lt-clean-bar")
    assert f.data["ai_metadata"]["description"] == f.data["ai_metadata"]["short_summary"]
    assert f.data["ai_metadata"]["analyzed"] is True
    assert f.data["ai_metadata"]["source"] == "hyperframes_motion_graphics"
    assert f.data["ai_metadata"]["transparent"] is True
    assert "Alex" in f.data.get("name", "")
    f.save.assert_called_once()


def test_fetch_resolves_label_from_job_when_empty():
    with patch.object(
        th,
        "_resolve_motion_graphics_label_from_job",
        return_value=("HyperFrames hw-title: HOLD.", {"transparent": False}),
    ) as resolve, patch.object(
        th,
        "_download_and_import_one",
        return_value=("F99", 1.2, None, False, "yuv420p"),
    ) as download, patch.object(th, "_motion_graphics_cleanup_storage"):
        msg = th.fetch_motion_graphics_video(
            segment_urls=["https://x/output.mp4"],
            render_job_id="job-99",
            label="",
        )
    resolve.assert_called_once_with("job-99", fallback="")
    assert "F99" in msg
    assert download.call_args.kwargs.get("label") == "HyperFrames hw-title: HOLD."
    assert download.call_args.kwargs.get("job_transparent") is False
    assert "transparent_ok=false" in msg
    assert "pix_fmt=yuv420p" in msg


def test_download_rejects_empty_file(tmp_path, monkeypatch):
    class _Resp:
        def read(self, n):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()), patch(
        "tempfile.mkdtemp", return_value=str(tmp_path)
    ):
        try:
            th._download_motion_graphics_file("https://x/output.mp4")
            assert False, "expected empty-file ValueError"
        except ValueError as e:
            assert "empty" in str(e).lower()


def test_fetch_rewrites_mp4_to_webm_when_job_transparent():
    with patch.object(
        th,
        "_resolve_motion_graphics_label_from_job",
        return_value=("HyperFrames lt-clean-bar: Alex (transparent overlay)", {"transparent": True}),
    ), patch.object(
        th,
        "_download_and_import_one",
        return_value=("F1", 0.2, None, True, "yuva420p"),
    ) as download, patch.object(th, "_motion_graphics_cleanup_storage"):
        msg = th.fetch_motion_graphics_video(
            segment_urls=["https://x/motion/j1/output.mp4"],
            render_job_id="j1",
            label="",
        )
    assert "F1" in msg
    called_url = download.call_args.args[0] if download.call_args.args else download.call_args[0][0]
    assert called_url.endswith(".webm")
    assert download.call_args.kwargs.get("job_transparent") is True
    assert "transparent_ok=true" in msg
    assert "pix_fmt=yuva420p" in msg


def test_fetch_transparent_webm_fail_no_mp4_fallback():
    with patch.object(
        th,
        "_resolve_motion_graphics_label_from_job",
        return_value=("HyperFrames lt-clean-bar: Title", {"transparent": True}),
    ), patch.object(
        th,
        "_download_and_import_one",
        return_value=("", 0.0, "alpha import failed (no opaque fallback): boom", False, ""),
    ) as download, patch.object(th, "_motion_graphics_cleanup_storage") as cleanup:
        msg = th.fetch_motion_graphics_video(
            segment_urls=["https://x/motion/j1/output.webm"],
            render_job_id="j1",
            label="HyperFrames lt-clean-bar: Title",
        )
    assert "failed" in msg.lower()
    assert "no opaque mp4 fallback" in msg.lower() or "re-compose" in msg.lower()
    assert download.call_count == 1
    cleanup.assert_not_called()


def test_fetch_warns_when_summary_generic():
    with patch.object(
        th,
        "_resolve_motion_graphics_label_from_job",
        return_value=("HyperFrames motion graphic", {}),
    ), patch.object(
        th,
        "_download_and_import_one",
        return_value=("F2", 1.0, None, False, "yuv420p"),
    ), patch.object(th, "_motion_graphics_cleanup_storage"):
        msg = th.fetch_motion_graphics_video(
            segment_urls=["https://x/output.mp4"],
            render_job_id="j2",
            label="",
        )
    assert "short_summary is generic" in msg


def test_download_motion_graphics_preserves_webm_ext():
    class _Resp:
        def read(self, n):
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return b"webm-bytes"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        path, size = th._download_motion_graphics_file(
            "https://x.supabase.co/storage/v1/object/public/product_demo/motion/j1/output.webm"
        )
    assert path.endswith(".webm")
    assert size > 0
