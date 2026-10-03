"""Hardware encoder probe and selection for export."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from functools import lru_cache
from typing import Callable, Optional

from classes.encoder_trial import TRIAL_FLAG
from classes.logger import log

# Software fallback — always available when FFmpegWriter works at all.
SOFTWARE_H264 = "libx264"

# Platform-ordered H.264 hardware encoder candidates.
_PLATFORM_H264 = {
    "darwin": ["h264_videotoolbox", SOFTWARE_H264],
    "win32": ["h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", SOFTWARE_H264],
    "linux": ["h264_nvenc", "h264_vaapi", "h264_qsv", SOFTWARE_H264],
}

# Software encoder that replaces a hardware one of the same family.
_SOFTWARE_BY_FAMILY = (
    ("h264", SOFTWARE_H264),
    ("hevc", "libx265"),
    ("vp9", "libvpx-vp9"),
    ("av1", "libsvtav1"),
)

# The trial is a fresh process that has to load libopenshot before it encodes
# anything: a cold import alone took 13 s on a loaded 8 GB Mac.
TRIAL_TIMEOUT_SECONDS = 90

# codec -> Future[bool]; one trial per codec per session.
_trials: dict[str, Future] = {}
_trials_lock = threading.Lock()


def platform_encoder_candidates(codec_family: str = "h264") -> list[str]:
    if codec_family != "h264":
        return [SOFTWARE_H264]
    return list(_PLATFORM_H264.get(sys.platform, [SOFTWARE_H264]))


def probe_video_encoder(codec: str) -> bool:
    """Return True if FFmpegWriter can encode with *codec*.

    IsValidCodec only asks FFmpeg whether an encoder by that name exists. A
    hardware encoder can exist and still abort the process on its first frame,
    so hardware encoders must also pass a trial encode (see encoder_passes_trial).
    """
    try:
        import openshot

        if not openshot.FFmpegWriter.IsValidCodec(str(codec)):
            return False
    except Exception as exc:
        log.debug("Encoder probe failed for %s: %s", codec, exc)
        return False
    return not is_hardware_encoder(codec) or encoder_passes_trial(codec)


def _trial_command(codec: str) -> list[str]:
    """argv that starts a copy of the app in trial-encode mode (src/launch.py)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, TRIAL_FLAG, codec]
    from classes import info

    return [sys.executable, os.path.join(info.PATH, "launch.py"), TRIAL_FLAG, codec]


def _run_trial(codec: str) -> bool:
    """Encode a few frames with *codec* in a child process. True if it exited 0.

    The child is where an abort() lands, so a broken hardware encoder costs a
    log line here instead of the whole app.
    """
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    started = time.monotonic()
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                _trial_command(codec),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
                **kwargs,
            )
        except OSError as exc:
            log.warning("Could not start a trial encode for %s: %s", codec, exc)
            return False
        try:
            code = proc.wait(timeout=TRIAL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            log.warning(
                "Hardware video encoder %s: trial encode did not finish in %ss",
                codec, TRIAL_TIMEOUT_SECONDS,
            )
            return False
        if code == 0:
            log.info(
                "Hardware video encoder %s passed a trial encode (%.1fs)",
                codec, time.monotonic() - started,
            )
            return True
        err.seek(0)
        log.warning(
            "Hardware video encoder %s failed a trial encode (exit %s): %s",
            codec, code, _failure_detail(err.read().decode("utf-8", "replace")),
        )
        return False


def _failure_detail(stderr_text: str) -> str:
    """The lines of a failed trial's stderr worth logging (not the stack dump)."""
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    keep = [
        line for line in lines
        if any(t in line for t in ("Assertion", "Caught signal", "failed", "rror"))
    ]
    return " | ".join((keep or lines[-3:])[:4])[:600]


def _resolve_trial(codec: str, future: Future) -> None:
    try:
        future.set_result(_run_trial(codec))
    except Exception:
        # A broken probe must never select the hardware path.
        log.warning("Trial encode for %s raised", codec, exc_info=True)
        future.set_result(False)


def encoder_passes_trial(
    codec: str, *, poll: Optional[Callable[[], None]] = None
) -> bool:
    """True when *codec* survived a trial encode in a child process.

    The result is cached for the session. The trial runs on its own
    "zenvi-encoder-trial" thread; *poll*, when given, is called every 50 ms
    while waiting, so a GUI-thread caller can keep its event loop turning.
    """
    with _trials_lock:
        future = _trials.get(codec)
        if future is None:
            future = Future()
            _trials[codec] = future
            threading.Thread(
                target=_resolve_trial,
                args=(codec, future),
                name="zenvi-encoder-trial",
                daemon=True,
            ).start()
    if poll is None:
        return bool(future.result())
    while True:
        try:
            return bool(future.result(timeout=0.05))
        except FutureTimeout:
            poll()


def software_fallback_encoder(codec: str) -> str:
    """The software encoder for *codec*'s family (h264_videotoolbox -> libx264)."""
    name = (codec or "").lower()
    for family, software in _SOFTWARE_BY_FAMILY:
        if name.startswith(family):
            return software
    return SOFTWARE_H264


def safe_video_encoder(
    codec: str, *, poll: Optional[Callable[[], None]] = None
) -> str:
    """*codec*, or its software equivalent if it is a hardware encoder that fails a trial."""
    if not is_hardware_encoder(codec) or encoder_passes_trial(codec, poll=poll):
        return codec
    fallback = software_fallback_encoder(codec)
    try:
        import openshot

        fallback_exists = bool(openshot.FFmpegWriter.IsValidCodec(fallback))
    except Exception:
        fallback_exists = True  # cannot tell here; the writer will report it
    if not fallback_exists:
        raise RuntimeError(
            "Hardware video encoder %s cannot encode on this system, and its "
            "software equivalent %s is not available. Choose another video codec."
            % (codec, fallback)
        )
    log.warning(
        "Hardware video encoder %s cannot encode on this system; exporting with %s",
        codec, fallback,
    )
    return fallback


@lru_cache(maxsize=16)
def get_preferred_video_encoder(
    codec_family: str = "h264",
    *,
    prefer_hardware: bool = True,
) -> str:
    """Return the first working encoder for this platform.

    When prefer_hardware is False, always returns libx264 (final-delivery path).
    """
    if not prefer_hardware:
        return SOFTWARE_H264
    for codec in platform_encoder_candidates(codec_family):
        if codec == SOFTWARE_H264 or probe_video_encoder(codec):
            if codec != SOFTWARE_H264:
                log.info("Selected hardware video encoder: %s", codec)
            return codec
    return SOFTWARE_H264


def is_hardware_encoder(codec: str) -> bool:
    name = (codec or "").lower()
    return any(
        token in name
        for token in ("nvenc", "videotoolbox", "vaapi", "qsv", "amf", "mf", "dxva")
    )


def hardware_bitrate_multiplier(codec: str) -> float:
    """Compensate for HW encoders' weaker quality-per-bit vs libx264 medium.

    Returns a multiplier applied to the requested bitrate. Software stays 1.0.
    """
    if not is_hardware_encoder(codec):
        return 1.0
    # Roughly match x264 medium visual quality with a modest size bump.
    return 1.35


def maybe_apply_hardware_bitrate(video_settings: dict, *, prefer_hardware: bool = True) -> dict:
    """Return a copy of video_settings with vcodec/bitrate adjusted for HW encode."""
    out = dict(video_settings)
    current = str(out.get("vcodec") or SOFTWARE_H264)
    # Only auto-replace the software default; never rewrite a user-chosen codec.
    if current == SOFTWARE_H264 and prefer_hardware:
        selected = get_preferred_video_encoder(prefer_hardware=True)
        out["vcodec"] = selected
    else:
        selected = current
    mult = hardware_bitrate_multiplier(selected)
    if mult != 1.0:
        try:
            bitrate = int(out.get("video_bitrate") or 0)
            if bitrate > 0:
                out["video_bitrate"] = int(round(bitrate * mult))
        except Exception:
            pass
    return out


def clear_encoder_probe_cache() -> None:
    get_preferred_video_encoder.cache_clear()
    with _trials_lock:
        _trials.clear()
