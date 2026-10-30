"""Alpha-preserving import and metadata stamping for HyperFrames renders (no Qt app)."""

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


def test_import_generated_video_calls_add_files_with_skip_indexing(monkeypatch):
    """MG/AI import must not enqueue Gemini indexing."""
    fake_file = SimpleNamespace(id="F1", data={}, absolute_path=lambda: "/tmp/out.mp4")
    fake_query = MagicMock()
    fake_query.File.get.return_value = fake_file
    fake_query.File.filter.return_value = []
    monkeypatch.setitem(sys.modules, "classes.query", fake_query)

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


