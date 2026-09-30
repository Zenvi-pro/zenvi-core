"""Hardware decode probe, one-time auto-enable, and runtime fallback."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Callable, Iterable, Optional

from classes import info
from classes.logger import log

# libopenshot HARDWARE_DECODER enum values (matches Preferences dropdown).
HW_NONE = 0
HW_VAAPI = 1
HW_NVDEC = 2
HW_D3D9 = 3
HW_D3D11 = 4
HW_VIDEOTOOLBOX = 5
HW_VDPAU = 6
HW_QSV = 7

# Default size ceiling after Phase 1 (was 1950x1100 — blocked all 4K).
DEFAULT_HW_MAX_WIDTH = 8192
DEFAULT_HW_MAX_HEIGHT = 4320


def platform_decoder_candidates() -> list[int]:
    """Ordered decoder candidates for the current OS (best first)."""
    if sys.platform == "darwin":
        return [HW_VIDEOTOOLBOX]
    if sys.platform == "win32":
        return [HW_NVDEC, HW_D3D11, HW_QSV, HW_D3D9]
    # Linux and others
    return [HW_NVDEC, HW_VAAPI, HW_QSV, HW_VDPAU]


def hardware_example_path() -> str:
    return os.path.join(info.RESOURCES_PATH, "hardware-example.mp4")


def probe_hardware_decoder(
    decoder: int,
    *,
    device_index: int = 0,
    example_path: Optional[str] = None,
) -> bool:
    """Return True if *decoder* correctly decodes the bundled example clip.

    Mutates process-global openshot.Settings temporarily; always restores.
    Must run before timelines are opened for real work.
    """
    try:
        import openshot
    except Exception as exc:
        log.debug("openshot unavailable for HW decode probe: %s", exc)
        return False

    path = example_path or hardware_example_path()
    if not os.path.isfile(path):
        log.warning("HW decode probe asset missing: %s", path)
        return False

    settings = openshot.Settings.Instance()
    previous_decoder = settings.HARDWARE_DECODER
    previous_device = getattr(settings, "HW_DE_DEVICE_SET", 0)
    clip = None
    reader = None
    try:
        settings.HARDWARE_DECODER = int(decoder)
        if hasattr(settings, "HW_DE_DEVICE_SET"):
            settings.HW_DE_DEVICE_SET = int(device_index)
        clip = openshot.Clip(path)
        reader = clip.Reader()
        reader.Open()
        # Same pixel oracle as Preferences.testHardwareDecode.
        ok = bool(reader.GetFrame(0).CheckPixel(0, 0, 2, 133, 255, 255, 5))
        return ok
    except Exception as exc:
        log.debug("HW decode probe failed for decoder=%s: %s", decoder, exc)
        return False
    finally:
        try:
            if reader is not None:
                reader.Close()
        except Exception:
            pass
        try:
            if clip is not None:
                clip.Close()
        except Exception:
            pass
        try:
            settings.HARDWARE_DECODER = previous_decoder
            if hasattr(settings, "HW_DE_DEVICE_SET"):
                settings.HW_DE_DEVICE_SET = previous_device
        except Exception:
            pass


def detect_best_hardware_decoder(
    candidates: Optional[Iterable[int]] = None,
    *,
    device_index: int = 0,
) -> int:
    """Probe candidates and return the first that works, or HW_NONE."""
    for decoder in candidates if candidates is not None else platform_decoder_candidates():
        if probe_hardware_decoder(int(decoder), device_index=device_index):
            log.info("Hardware decode probe succeeded: decoder=%s", decoder)
            return int(decoder)
        log.debug("Hardware decode probe rejected decoder=%s", decoder)
    log.info("Hardware decode probe: no accelerator available; using software")
    return HW_NONE


def maybe_auto_detect_hardware_decoder(settings_store) -> Optional[int]:
    """One-time migration: probe and persist hw-decoder for existing installs.

    Returns the decoder value applied, or None if migration was already done
    / skipped. Never overwrites an explicit user choice after the flag is set.
    """
    if settings_store is None:
        return None
    try:
        already = settings_store.get("hardwareAutoDetectApplied")
    except Exception:
        already = False
    if already:
        return None

    # Probe before timelines open.
    detected = detect_best_hardware_decoder(
        device_index=int(str(settings_store.get("graca_number_de") or 0)),
    )
    settings_store.set("hw-decoder", str(detected))
    settings_store.set("decode_hw_max_width", DEFAULT_HW_MAX_WIDTH)
    settings_store.set("decode_hw_max_height", DEFAULT_HW_MAX_HEIGHT)
    settings_store.set("hardwareAutoDetectApplied", True)
    log.info(
        "One-time hardware decode auto-detect applied: hw-decoder=%s, max=%sx%s",
        detected,
        DEFAULT_HW_MAX_WIDTH,
        DEFAULT_HW_MAX_HEIGHT,
    )
    return detected


def apply_hardware_decode_settings(settings_store) -> None:
    """Push hw-decoder / size limits from settings into libopenshot."""
    try:
        import openshot
    except Exception:
        return
    lib = openshot.Settings.Instance()
    try:
        lib.HARDWARE_DECODER = int(str(settings_store.get("hw-decoder") or 0))
    except Exception:
        lib.HARDWARE_DECODER = 0
    try:
        lib.HW_DE_DEVICE_SET = int(str(settings_store.get("graca_number_de") or 0))
    except Exception:
        pass
    try:
        lib.DE_LIMIT_WIDTH_MAX = int(str(settings_store.get("decode_hw_max_width") or DEFAULT_HW_MAX_WIDTH))
        lib.DE_LIMIT_HEIGHT_MAX = int(str(settings_store.get("decode_hw_max_height") or DEFAULT_HW_MAX_HEIGHT))
    except Exception:
        pass


@contextmanager
def with_software_decode_fallback(on_fallback: Optional[Callable[[Exception], None]] = None):
    """Temporarily force software decode after a hardware decode failure.

    Usage around a decode that may throw:
        try:
            ...
        except Exception as exc:
            with with_software_decode_fallback():
                retry...
    """
    try:
        import openshot
    except Exception:
        yield
        return

    settings = openshot.Settings.Instance()
    previous = settings.HARDWARE_DECODER
    settings.HARDWARE_DECODER = HW_NONE
    try:
        yield
    finally:
        try:
            settings.HARDWARE_DECODER = previous
        except Exception:
            pass


def force_software_decode() -> int:
    """Set HARDWARE_DECODER to software and return the previous value."""
    try:
        import openshot

        settings = openshot.Settings.Instance()
        previous = int(settings.HARDWARE_DECODER)
        settings.HARDWARE_DECODER = HW_NONE
        return previous
    except Exception:
        return HW_NONE
