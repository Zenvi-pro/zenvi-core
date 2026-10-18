"""Low-end machine performance defaults (#267, #320)."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from classes.export_acceleration.background_render import BackgroundRenderManager
from classes.export_acceleration.hw_decode import maybe_auto_detect_hardware_decoder

DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "src", "settings", "_default.settings")


def _default(name):
    with open(DEFAULTS, encoding="utf-8") as f:
        return next(s["value"] for s in json.load(f) if s.get("setting") == name)


def test_auto_detect_never_overrides_an_existing_install(monkeypatch):
    # The flag can be lost (Restore Defaults, an older build saving the file);
    # an install that already had a settings file keeps its decoder choice.
    store = MagicMock()
    store.user_file_existed = True
    store.get.side_effect = lambda key: False if key == "hardwareAutoDetectApplied" else "0"
    probe = MagicMock(return_value=4)
    monkeypatch.setattr("classes.export_acceleration.hw_decode.detect_best_hardware_decoder", probe)

    assert maybe_auto_detect_hardware_decoder(store) is None
    probe.assert_not_called()
    assert [c.args[0] for c in store.set.call_args_list] == ["hardwareAutoDetectApplied"]


def test_restore_performance_defaults_keeps_the_one_time_flag(tmp_path, monkeypatch):
    pytest.importorskip("PyQt5.QtWidgets")
    pytest.importorskip("openshot")  # classes.settings imports it
    from classes import info
    from classes.settings import SettingStore

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    (tmp_path / "openshot.settings").write_text(json.dumps([
        {"setting": "hardwareAutoDetectApplied", "value": True, "category": "Performance"},
        {"setting": "hw-decoder", "value": "0", "category": "Performance"},
    ]), encoding="utf-8")
    store = SettingStore()
    store.load()
    store.restore("Performance")
    assert store.get("hardwareAutoDetectApplied") is True


def test_background_cache_warming_is_off_by_default():
    assert _default("backgroundCacheWarming") is False


def test_cache_warming_renders_each_frame_once_then_idles():
    # It used to wrap to frame 1 forever, re-rendering (and evicting) the cache
    # and holding the timeline lock the GUI thread needs to seek.
    requested = []
    timeline = MagicMock()
    timeline.GetMaxFrame.return_value = 30
    timeline.GetFrame.side_effect = requested.append
    mgr = BackgroundRenderManager(
        get_timeline=lambda: timeline,
        get_project_data=dict,
        is_user_busy=lambda: False,
    )
    for _ in range(10):
        mgr._warm_one_chunk()
    assert sorted(requested) == list(range(1, 31))

    mgr.invalidate()  # an edit re-arms one more pass
    mgr._warm_one_chunk()
    assert requested[30] == 1


def test_heavy_footage_gets_an_optimized_preview_on_import():
    pytest.importorskip("PyQt5.QtWidgets")
    pytest.importorskip("openshot")
    from types import SimpleNamespace
    from windows.models.files_model import needs_optimized_preview

    def file(media_type, width, height):
        return SimpleNamespace(data={"media_type": media_type, "width": width, "height": height})

    assert needs_optimized_preview(file("video", 3840, 2160))
    assert needs_optimized_preview(file("video", 2560, 1440))
    assert not needs_optimized_preview(file("video", 1920, 1080))
    assert not needs_optimized_preview(file("image", 6000, 4000))
    assert _default("optimize-preview-auto") is True


def test_cache_warming_resumes_where_a_busy_user_interrupted_it():
    # Marking the pass done before the frames rendered skipped the rest of the last chunk
    requested, busy = [], {"now": False}
    timeline = MagicMock()
    timeline.GetMaxFrame.return_value = 10

    def get_frame(n):
        requested.append(n)
        if n == 5:
            busy["now"] = True  # the user starts scrubbing mid-chunk
    timeline.GetFrame.side_effect = get_frame
    mgr = BackgroundRenderManager(get_timeline=lambda: timeline, get_project_data=dict,
                                  is_user_busy=lambda: busy["now"])
    mgr._warm_one_chunk()
    busy["now"] = False
    mgr._warm_one_chunk()
    mgr._warm_one_chunk()
    assert sorted(set(requested)) == list(range(1, 11))
