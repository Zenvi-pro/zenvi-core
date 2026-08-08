"""Unit tests for HyperFrames fetch metadata stamping (no Qt app)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_FIX_MG = Path(__file__).resolve().parent / "fixtures" / "motion_graphics"
if str(_FIX_MG.parent) not in sys.path:
    sys.path.insert(0, str(_FIX_MG.parent))

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

from motion_graphics.build_vp9 import (  # noqa: E402
    build_vp9_alpha_overlay,
    build_vp9_opaque_plate,
    ensure_ffmpeg_on_path,
    load_alpha_probe_contract,
    require_ffmpeg_libvpx,
)

# tool_handlers calls bare "ffmpeg"/"ffprobe" — ensure MSYS bins resolve under Windows pytest.
ensure_ffmpeg_on_path()


def _ffmpeg_available():
    try:
        require_ffmpeg_libvpx()
        return True
    except RuntimeError:
        return False


requires_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg + libvpx-vp9 required for VP9 alpha fixtures",
)


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
        th, "_ffprobe_has_explicit_yuva", return_value=False
    ), patch.object(
        th, "_reencode_alpha_for_openshot", return_value=(None, "vp9 alpha failed")
    ), patch.object(th, "_reencode_for_openshot") as opaque:
        f, err = th._import_generated_video("/tmp/src.webm", preserve_alpha=True)
    assert f is None
    assert err is not None
    assert "no opaque fallback" in err.lower() or "alpha import failed" in err.lower()
    opaque.assert_not_called()


def test_ffprobe_has_alpha_accepts_alpha_mode_tag():
    with patch.object(th, "_ffprobe_pix_fmt", return_value="yuv420p"), patch.object(
        th, "_ffprobe_alpha_mode", return_value="1"
    ):
        assert th._ffprobe_has_alpha("/tmp/x.webm") is True
    with patch.object(th, "_ffprobe_pix_fmt", return_value="yuv420p"), patch.object(
        th, "_ffprobe_alpha_mode", return_value=""
    ):
        assert th._ffprobe_has_alpha("/tmp/x.webm") is False
    with patch.object(th, "_ffprobe_pix_fmt", return_value="yuva420p"), patch.object(
        th, "_ffprobe_alpha_mode", return_value=""
    ):
        assert th._ffprobe_has_alpha("/tmp/x.webm") is True


def test_openshot_transparent_ok_accepts_explicit_yuva():
    with patch.object(th, "_ffprobe_pix_fmt", return_value="yuva420p"), patch.object(
        th, "_verify_decoded_alpha_pixels", return_value=True
    ):
        assert th._openshot_transparent_ok("/tmp/x.webm") is True


def test_openshot_transparent_ok_requires_pixels_when_alpha_mode():
    with patch.object(th, "_ffprobe_pix_fmt", return_value="argb"), patch.object(
        th, "_verify_decoded_alpha_pixels", return_value=True
    ):
        assert th._openshot_transparent_ok("/tmp/x.mov") is True
    with patch.object(th, "_ffprobe_pix_fmt", return_value="argb"), patch.object(
        th, "_verify_decoded_alpha_pixels", return_value=False
    ):
        assert th._openshot_transparent_ok("/tmp/x.mov") is False
    # VP9 WebM sidecar alpha is never OpenShot-ok (native = solid black)
    with patch.object(th, "_ffprobe_pix_fmt", return_value="yuv420p"):
        assert th._openshot_transparent_ok("/tmp/x.webm") is False


def test_reencode_alpha_uses_libvpx_decoder_before_input():
    with patch.object(th, "_ffmpeg_run", return_value=(True, "")) as run, patch.object(
        th, "_verify_decoded_alpha_pixels", return_value=True
    ):
        out, err = th._reencode_alpha_for_openshot("/tmp/in.webm", output_path="/tmp/out.mov")
    assert err is None
    assert out == "/tmp/out.mov"
    cmd = run.call_args.args[0]
    # Decoder before -i, qtrle encoder after (OpenShot-native alpha)
    i_idx = cmd.index("-i")
    assert cmd[i_idx - 2 : i_idx] == ["-c:v", "libvpx-vp9"]
    assert "qtrle" in cmd
    assert "argb" in cmd
    assert out.endswith(".mov")


def test_reencode_alpha_forces_mov_extension():
    with patch.object(th, "_ffmpeg_run", return_value=(True, "")) as run, patch.object(
        th, "_verify_decoded_alpha_pixels", return_value=True
    ):
        out, err = th._reencode_alpha_for_openshot("/tmp/in.webm", output_path="/tmp/out.webm")
    assert err is None
    assert out.endswith(".mov")
    assert run.call_args.args[0][-1].endswith(".mov")


def test_reencode_alpha_fails_when_pixels_opaque():
    with patch.object(th, "_ffmpeg_run", return_value=(True, "")), patch.object(
        th, "_verify_decoded_alpha_pixels", return_value=False
    ):
        out, err = th._reencode_alpha_for_openshot("/tmp/in.webm", output_path="/tmp/out.webm")
    assert out is None
    assert err is not None
    assert "opaque" in err.lower()


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
        return_value=("F1", 0.2, None, True, "yuv420p;alpha_mode=1"),
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
    assert "alpha_mode=1" in msg or "yuv420p" in msg


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


def test_alpha_probe_contract_json_shape():
    contract = load_alpha_probe_contract()
    assert contract["vp9_alpha_webm"]["expect_pix_fmt"] == "yuv420p"
    assert contract["vp9_alpha_webm"]["expect_alpha_mode"] == "1"
    assert contract["vp9_alpha_webm"]["expect_explicit_yuva"] is False
    assert contract["vp9_alpha_webm"]["expect_native_ok"] is False
    assert contract["openshot_qtrle_mov"]["expect_ext"] == ".mov"
    assert contract["openshot_qtrle_mov"]["expect_openshot_ok"] is True
    assert contract["vp9_opaque_webm"]["expect_openshot_ok"] is False


@requires_ffmpeg
def test_vp9_alpha_probe_is_yuv420p_not_yuva(tmp_path):
    """libvpx WebM never reports yuva* — ALPHA_MODE=1 is the real probe shape."""
    contract = load_alpha_probe_contract()["vp9_alpha_webm"]
    path = build_vp9_alpha_overlay(tmp_path / "vp9_alpha_overlay.webm")
    pix = th._ffprobe_pix_fmt(str(path))
    mode = th._ffprobe_alpha_mode(str(path))
    assert pix == contract["expect_pix_fmt"]
    assert mode == contract["expect_alpha_mode"]
    assert th._ffprobe_has_explicit_yuva(str(path)) is contract["expect_explicit_yuva"]
    assert th._ffprobe_has_alpha(str(path)) is True


@requires_ffmpeg
def test_openshot_transparent_ok_accepts_alpha_mode(tmp_path):
    """qtrle MOV (post re-encode) is OpenShot-ok; raw VP9 WebM is not for native path."""
    src = build_vp9_alpha_overlay(tmp_path / "vp9_alpha_overlay.webm")
    mov = tmp_path / "overlay.mov"
    clean, err = th._reencode_alpha_for_openshot(str(src), output_path=str(mov), width=320, height=180)
    assert err is None, err
    assert th._openshot_transparent_ok(clean) is True
    assert th._verify_decoded_alpha_pixels(clean, force_libvpx=False) is True


@requires_ffmpeg
def test_openshot_transparent_ok_rejects_opaque(tmp_path):
    path = build_vp9_opaque_plate(tmp_path / "vp9_opaque_plate.webm")
    contract = load_alpha_probe_contract()["vp9_opaque_webm"]
    assert th._ffprobe_pix_fmt(str(path)) == contract["expect_pix_fmt"]
    assert (th._ffprobe_alpha_mode(str(path)) or "") == contract["expect_alpha_mode"]
    assert th._openshot_transparent_ok(str(path)) is False


@requires_ffmpeg
def test_reencode_preserves_decoded_alpha(tmp_path):
    src = build_vp9_alpha_overlay(tmp_path / "src_alpha.webm")
    out = tmp_path / "reencoded.mov"
    clean, err = th._reencode_alpha_for_openshot(str(src), output_path=str(out), width=320, height=180)
    assert err is None, err
    assert clean and Path(clean).is_file()
    assert clean.lower().endswith(".mov")
    pix = th._ffprobe_pix_fmt(clean)
    assert pix and ("argb" in pix or "rgba" in pix or "yuva" in pix)
    # Native decode (OpenShot path) must see transparency — not only libvpx
    assert th._verify_decoded_alpha_pixels(clean, force_libvpx=False) is True
    assert th._openshot_transparent_ok(clean) is True


@requires_ffmpeg
def test_vp9_webm_native_decode_is_opaque_black(tmp_path):
    """Documents why we must not import VP9 WebM into OpenShot as-is."""
    src = build_vp9_alpha_overlay(tmp_path / "src_alpha.webm")
    assert th._verify_decoded_alpha_pixels(str(src), force_libvpx=True) is True
    # Native decode drops alpha → opaque (OpenShot's FFmpegReader behavior)
    assert th._verify_decoded_alpha_pixels(str(src), force_libvpx=False) is False


@requires_ffmpeg
def test_download_and_import_transparent_accepts_alpha_mode(tmp_path):
    """job_transparent=True must accept qtrle MOV after import (not refuse on WebM probe)."""
    src = build_vp9_alpha_overlay(tmp_path / "dl_alpha.webm")
    mov = tmp_path / "imported.mov"
    clean, err = th._reencode_alpha_for_openshot(str(src), output_path=str(mov), width=320, height=180)
    assert err is None, err
    fake_file = SimpleNamespace(
        id="FALPHA",
        data={"path": clean},
        absolute_path=lambda: clean,
        save=MagicMock(),
    )
    with patch.object(th, "_download_motion_graphics_file", return_value=(str(src), 0.1)), patch.object(
        th, "_import_generated_video", return_value=(fake_file, None)
    ), patch.object(th, "_stamp_motion_graphics_file_metadata") as stamp:
        file_id, size_mb, err, transparent_ok, probe = th._download_and_import_one(
            "https://x/output.webm",
            label="HyperFrames lt-clean-bar: HOLD",
            job_transparent=True,
        )
    assert err is None, err
    assert file_id == "FALPHA"
    assert transparent_ok is True
    assert "argb" in probe or "rgba" in probe or "yuva" in probe
    stamp.assert_called_once()
    assert stamp.call_args.kwargs.get("transparent") is True


@requires_ffmpeg
def test_download_and_import_transparent_refuses_opaque_plate(tmp_path):
    src = build_vp9_opaque_plate(tmp_path / "dl_opaque.webm")
    fake_file = SimpleNamespace(
        id="FOPAQUE",
        data={"path": str(src)},
        absolute_path=lambda: str(src),
        save=MagicMock(),
    )
    with patch.object(th, "_download_motion_graphics_file", return_value=(str(src), 0.1)), patch.object(
        th, "_import_generated_video", return_value=(fake_file, None)
    ), patch.object(th, "_stamp_motion_graphics_file_metadata") as stamp:
        file_id, _size, err, transparent_ok, _probe = th._download_and_import_one(
            "https://x/output.webm",
            label="bad",
            job_transparent=True,
        )
    assert file_id == ""
    assert transparent_ok is False
    assert err is not None
    assert "usable VP9 alpha" in err or "refusing solid plate" in err
    stamp.assert_not_called()
