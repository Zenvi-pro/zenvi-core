"""
 @file
 @brief Probe an import's source media off the GUI thread, once per file.
"""

import copy
import json


def probe_clip(path, cache, openshot_module):
    """``(clip_json, reader_json)`` for *path*; *reader_json* is None if it has no reader.

    Opening media (libopenshot readers, Qt image/SVG decoding) is the slow part
    of an EDL / XML import. It runs through run_off_gui -- a worker thread, with
    the editor still painting -- and only once per file per import (*cache*):
    an edit that cuts one file into fifty clips opens it once. Returns copies,
    so each clip can change its own.
    """
    from classes.qt_main_thread import run_off_gui

    if path not in cache:
        def _probe():
            clip_obj = openshot_module.Clip(path)
            try:
                reader = json.loads(clip_obj.Reader().Json())
            except Exception:
                reader = None
            return json.loads(clip_obj.Json()), reader

        cache[path] = run_off_gui(_probe)
    clip_json, reader_json = cache[path]
    return copy.deepcopy(clip_json), copy.deepcopy(reader_json)
