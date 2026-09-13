"""Hardware encoder probe and selection for export."""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Optional

from classes.logger import log

# Software fallback — always available when FFmpegWriter works at all.
SOFTWARE_H264 = "libx264"

# Platform-ordered H.264 hardware encoder candidates.
_PLATFORM_H264 = {
    "darwin": ["h264_videotoolbox", SOFTWARE_H264],
    "win32": ["h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", SOFTWARE_H264],
    "linux": ["h264_nvenc", "h264_vaapi", "h264_qsv", SOFTWARE_H264],
}


def platform_encoder_candidates(codec_family: str = "h264") -> list[str]:
    if codec_family != "h264":
        return [SOFTWARE_H264]
    return list(_PLATFORM_H264.get(sys.platform, [SOFTWARE_H264]))


def probe_video_encoder(codec: str) -> bool:
    """Return True if FFmpegWriter reports *codec* as valid."""
    try:
        import openshot

        return bool(openshot.FFmpegWriter.IsValidCodec(str(codec)))
    except Exception as exc:
        log.debug("Encoder probe failed for %s: %s", codec, exc)
        return False


@lru_cache(maxsize=16)
def get_preferred_video_encoder(
    codec_family: str = "h264",
    *,
    prefer_hardware: bool = True,
) -> str:
    """Return the first valid encoder for this platform.

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
