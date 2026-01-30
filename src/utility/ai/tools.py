"""
LangChain tools for video editing operations.
"""
from typing import Optional

from langchain.tools import tool
from classes.logger import log


@tool
def export_video(width: int, height: int, output_path: Optional[str] = None) -> str:
    """
    Export the current video project at the specified resolution.
    
    This schedules the export operation to run on the Qt main thread using
    QMetaObject.invokeMethod with a queued connection so that all Qt
    interactions occur on the GUI thread (avoids threading issues when called
    from the AI worker thread).

    Args:
        width: Video width in pixels (e.g., 1920)
        height: Video height in pixels (e.g., 1080)
        output_path: Optional output file path. If not provided, uses default location.
    
    Returns:
        Status message about the export
    """
    try:
        from PyQt5.QtCore import QMetaObject, Qt, Q_ARG
        from classes.app import get_app

        main_window = getattr(get_app(), "window", None)
        if not main_window:
            return "Error: Video editor window not found"

        # Prepare arguments
        output_arg = output_path or ""

        # Prefer emitting the thread-safe signal if available (guaranteed to run slot on GUI thread)
        try:
            if hasattr(main_window, 'exportRequested'):
                try:
                    main_window.exportRequested.emit(int(width), int(height), str(output_arg))
                    log.info("Export scheduled via exportRequested.emit")
                    return f"Export scheduled at {width}x{height}. Check the Export dialog for progress."
                except Exception as exc_emit:
                    log.warning(f"exportRequested.emit failed: {exc_emit}")

            # Fallback: try QMetaObject.invokeMethod (may fail on some PyQt builds)
            success = QMetaObject.invokeMethod(
                main_window,
                "start_export_with_params",
                Qt.QueuedConnection,
                Q_ARG(int, int(width)),
                Q_ARG(int, int(height)),
                Q_ARG(str, str(output_arg))
            )

            if success:
                log.info(f"Export scheduled via QMetaObject.invokeMethod: {width}x{height}")
                return f"Export scheduled at {width}x{height}. Check the Export dialog for progress."

            # Last-resort fallback: schedule a QTimer callback on GUI thread
            log.error("Failed to schedule export on main thread (invokeMethod returned False). Trying fallback via QTimer.singleShot")
            try:
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(0, lambda: main_window.start_export_with_params(int(width), int(height), str(output_arg)))
                log.info("Export scheduled via QTimer.singleShot fallback")
                return f"Export scheduled (fallback) at {width}x{height}. Check the Export dialog for progress."
            except Exception as exc2:
                log.error(f"Fallback scheduling failed: {exc2}")
                return "Error: Failed to schedule export"

        except Exception as exc:
            log.error(f"Export scheduling error: {exc}")
            return f"Error: {exc}"

    except Exception as exc:
        log.error(f"Export tool error: {exc}")
        return f"Export error: {exc}"


def get_export_tools():
    """Return list of export-related tools for LangChain agent."""
    return [export_video]
