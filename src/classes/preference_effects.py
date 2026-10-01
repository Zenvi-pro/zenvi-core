"""What changing a preference does to the running editor (besides storing it).

The Preferences dialog and the agent preference tool both call
:func:`apply_preference_side_effects` after ``settings.set``, so a preference
takes effect the same way whichever of them changed it. Call it on the GUI
thread: it touches the main window, the theme and libopenshot settings.
"""

from __future__ import annotations

from classes.logger import log

CACHE_SETTINGS = frozenset({
    "cache-limit-mb", "cache-scale", "cache-quality", "cache-ahead-percent",
    "cache-preroll-min-frames", "cache-preroll-max-frames", "cache-max-frames",
    "cache-mode", "cache-image-format",
})


def thread_limits(setting_name):
    """(min, max) accepted for the OMP / FFmpeg thread-count preferences, else None."""
    from classes.settings import lib_default_thread_counts

    omp_default, ff_default = lib_default_thread_counts()
    if setting_name == "omp_threads_number":
        default_value = int(omp_default)
    elif setting_name == "ff_threads_number":
        default_value = int(ff_default)
    else:
        return None
    return 2, max(2, default_value * 3)


def apply_preference_side_effects(setting, value):
    """Apply one changed preference to the live session."""
    import openshot
    from classes.app import get_app

    app = get_app()
    window = getattr(app, "window", None)
    lib_settings = openshot.Settings.Instance()

    if setting == "debug-mode":
        openshot.ZmqLogger.Instance().Enable(bool(value))
    elif setting == "enable-auto-save" and window is not None:
        if value:
            window.auto_save_timer.start()
        else:
            window.auto_save_timer.stop()
    elif setting == "autosave-interval" and window is not None:
        window.auto_save_timer.setInterval(int(float(value) * 1000 * 60))
    elif setting in ("omp_threads_number", "ff_threads_number"):
        lo, hi = thread_limits(setting)
        count = max(lo, min(int(str(value)), hi))
        if setting == "omp_threads_number":
            from classes.settings import apply_openmp_settings
            lib_settings.OMP_THREADS = count
            apply_openmp_settings(lib_settings)
        else:
            lib_settings.FF_THREADS = count
    elif setting == "decode_hw_max_width":
        lib_settings.DE_LIMIT_WIDTH_MAX = int(str(value))
    elif setting == "decode_hw_max_height":
        lib_settings.DE_LIMIT_HEIGHT_MAX = int(str(value))
    elif setting == "hw-decoder":
        lib_settings.HARDWARE_DECODER = int(value)
    elif setting == "graca_number_de":
        lib_settings.HW_DE_DEVICE_SET = int(value)
    elif setting == "graca_number_en":
        lib_settings.HW_EN_DEVICE_SET = int(value)
    elif setting == "theme":
        if getattr(app, "theme_manager", None):
            app.theme_manager.apply_theme(value)
    elif setting == "timeline-thumbnail-style":
        timeline_widget = getattr(window, "timeline", None)
        if hasattr(timeline_widget, "set_thumbnail_style"):
            try:
                timeline_widget.set_thumbnail_style(value)
            except Exception:
                log.warning("Failed to apply timeline thumbnail style live", exc_info=1)

    if setting in CACHE_SETTINGS and window is not None:
        window.InitCacheSettings()
