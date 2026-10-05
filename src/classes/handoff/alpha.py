"""The alpha rule for linked media (SPEC §3.6), with ffmpeg helpers.

libopenshot 1.0 decodes VP9 WebM with its native decoder, which drops alpha
(a 50 % red VP9-alpha frame decodes with alpha 255), but keeps alpha for
ProRes 4444 (``yuva444p10le``) and QuickTime Animation (qtrle, ``argb``).
So a linked clip is never WebM:

* codec ``auto``: render one still first; :func:`choose_codec` picks
  ``prores4444`` when any pixel is not fully opaque, else ``h264``
  (CRF <= 18, yuv420p, BT.709 -- :data:`H264_ARGS`);
* a WebM from a third-party tool is re-encoded with
  :func:`reencode_to_prores4444` (decoded with libvpx-vp9, which keeps
  alpha) before it is linked.

All helpers run ffmpeg and block: call them off the GUI thread.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Callable, List, Optional

from classes import ffmpeg_cli

H264_ARGS = ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart"]
PRORES4444_ARGS = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-alpha_bits", "16",
                   "-vendor", "apl0"]
QTRLE_ARGS = ["-c:v", "qtrle", "-pix_fmt", "argb"]
_YMIN_RE = re.compile(r"lavfi\.signalstats\.YMIN=(\d+(?:\.\d+)?)")


class AlphaError(RuntimeError):
    """ffmpeg is missing or failed; the message says which."""


def _ffmpeg() -> str:
    exe = ffmpeg_cli.find_ffmpeg("ffmpeg")
    if not exe:
        raise AlphaError("ffmpeg was not found; install it (brew install ffmpeg) or set ZENVI_FFMPEG_DIR")
    return exe


def has_transparency(path: str, *, at_seconds: float = 0.0, vp9: Optional[bool] = None,
                     timeout: float = 60.0) -> bool:
    """True when the frame of *path* at *at_seconds* (a still, or a movie) has a pixel with alpha < 255.

    Media without an alpha channel is opaque. *vp9* forces the libvpx-vp9
    decoder (the one that reads WebM alpha); by default it is used for .webm.
    """
    use_vp9 = path.lower().endswith(".webm") if vp9 is None else vp9
    cmd: List[str] = [_ffmpeg(), "-v", "info", "-nostdin"]
    if use_vp9:
        cmd += ["-c:v", "libvpx-vp9"]
    if at_seconds:
        cmd += ["-ss", "%.3f" % float(at_seconds)]
    cmd += ["-i", path, "-frames:v", "1",
            "-vf", "format=rgba,alphaextract,signalstats,metadata=mode=print:key=lavfi.signalstats.YMIN",
            "-f", "null", "-"]
    try:
        proc = ffmpeg_cli.run_ffmpeg(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AlphaError(f"ffmpeg could not inspect {os.path.basename(path)}: {exc}") from None
    found = _YMIN_RE.findall((proc.stderr or "") + (proc.stdout or ""))
    if proc.returncode != 0 and not found:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-4:])
        raise AlphaError(f"ffmpeg could not read {os.path.basename(path)}: {tail}")
    return any(float(v) < 255 for v in found)


def choose_codec(still_path: str) -> str:
    """``prores4444`` when the probe still has transparency, else ``h264``."""
    return "prores4444" if has_transparency(still_path) else "h264"


def codec_args(codec: str) -> List[str]:
    """ffmpeg video encoder arguments for a linked-media codec."""
    if codec == "prores4444":
        return list(PRORES4444_ARGS)
    if codec == "qtrle":
        return list(QTRLE_ARGS)
    if codec == "h264":
        return list(H264_ARGS)
    raise ValueError(f"unknown linked-media codec {codec!r}")


def reencode_to_prores4444(src: str, dst: str, *, on_progress: Optional[Callable[[float], None]] = None,
                           should_cancel: Optional[Callable[[], bool]] = None, keep_audio: bool = True) -> str:
    """Re-encode *src* (WebM with alpha, or anything) to ProRes 4444 *dst* (.mov), keeping alpha.

    Writes ``dst + ".partial.mov"`` and renames it when complete. Returns *dst*.
    """
    if not dst.lower().endswith(".mov"):
        dst = os.path.splitext(dst)[0] + ".mov"
    partial = dst + ".partial.mov"
    cmd = [_ffmpeg(), "-y", "-nostdin"]
    if src.lower().endswith(".webm"):
        cmd += ["-c:v", "libvpx-vp9"]
    cmd += ["-i", src] + codec_args("prores4444")
    cmd += ["-c:a", "pcm_s16le"] if keep_audio else ["-an"]
    cmd.append(partial)
    result = ffmpeg_cli.run_ffmpeg_with_progress(cmd, on_progress=on_progress, should_cancel=should_cancel)
    if result.returncode != 0 or not os.path.isfile(partial):
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise AlphaError(f"re-encoding {os.path.basename(src)} to ProRes 4444 failed (ffmpeg exit {result.returncode})")
    os.replace(partial, dst)
    return dst
