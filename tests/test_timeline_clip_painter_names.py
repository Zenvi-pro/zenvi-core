"""The native timeline's clip painter must not hit NameError on its fallback paths.

_audio_thumbnail_pixmap used os without importing it (logged as NameError while
painting audio clips), and _finishItemResize used Clip without importing it.
"""

import importlib


def test_clip_painter_resolves_every_global_it_uses():
    mod = importlib.import_module("windows.views.timeline_backend.paint.clip")
    assert mod.os.path.join("a", "b")
    from classes.query import Clip
    assert mod.Clip is Clip
