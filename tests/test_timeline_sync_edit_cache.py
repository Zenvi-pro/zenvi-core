"""Edits invalidate only what changed (#267, #320, #332).

ApplyJsonDiff already removes the edited clip's old and new frame ranges and
clears the clip's own cache. The extra ClearAllCache after every edit threw the
whole cache away mid-playback, so dropping a clip while playing froze playback
and blocked the GUI thread on the timeline lock.

A media path / file_id swap is the exception: the frame ranges stay the same, so
ApplyJsonDiff alone leaves CacheMemory serving the previous file in preview
while export (fresh SetJson) shows the new one.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("openshot")

from classes import timeline as timeline_module  # noqa: E402


def _sync(monkeypatch, playing=False):
    settings = SimpleNamespace(ENABLE_PLAYBACK_CACHING=True)
    monkeypatch.setattr(timeline_module.openshot, "Settings",
                        SimpleNamespace(Instance=lambda: settings), raising=False)
    monkeypatch.setattr(timeline_module.openshot, "PLAYBACK_PLAY", 1, raising=False)
    sync = timeline_module.TimelineSync.__new__(timeline_module.TimelineSync)
    sync.timeline = MagicMock()
    sync.timeline_lock = MagicMock()
    mode = 1 if playing else 0
    sync.window = SimpleNamespace(
        preview_thread=SimpleNamespace(player=SimpleNamespace(Mode=lambda: mode)),
        proxy_service=None,
        refreshFrameSignal=MagicMock(),
    )
    return sync


def test_an_edit_does_not_clear_the_whole_cache(monkeypatch):
    sync = _sync(monkeypatch)
    action = SimpleNamespace(
        type="update",
        key=["clips", {"id": "C1"}],
        values={"id": "C1", "file_id": "F1", "reader": {"path": "/a.mp4"}},
        old_values={"id": "C1", "file_id": "F1", "reader": {"path": "/a.mp4"}},
        json=lambda **k: "[]",
    )

    sync.changed(action)

    sync.timeline.ApplyJsonDiff.assert_called_once()
    sync.timeline.ClearAllCache.assert_not_called()
    sync.window.refreshFrameSignal.emit.assert_called_once()


def test_relinking_a_clip_to_new_media_clears_the_preview_cache(monkeypatch):
    sync = _sync(monkeypatch)
    action = SimpleNamespace(
        type="update",
        key=["clips", {"id": "C1"}],
        values={"id": "C1", "file_id": "F2", "reader": {"path": "/broll_v2.mp4"}},
        old_values={"id": "C1", "file_id": "F1", "reader": {"path": "/broll_main.mp4"}},
        json=lambda **k: "[]",
    )

    sync.changed(action)

    sync.timeline.ApplyJsonDiff.assert_called_once()
    sync.timeline.ClearAllCache.assert_called_once_with(True)
    sync.window.refreshFrameSignal.emit.assert_called_once()


def test_media_source_changed_detects_path_file_id_and_proxy_swaps():
    same = SimpleNamespace(
        type="update", key=["clips", {"id": "C1"}],
        values={"file_id": "F1", "reader": {"path": r"C:\a\clip.mp4"}},
        old_values={"file_id": "F1", "reader": {"path": r"C:\a\clip.mp4"}},
    )
    assert not timeline_module.media_source_changed(same)

    by_path = SimpleNamespace(
        type="update", key=["clips", {"id": "C1"}],
        values={"file_id": "F1", "reader": {"path": "/new.mp4"}},
        old_values={"file_id": "F1", "reader": {"path": "/old.mp4"}},
    )
    assert timeline_module.media_source_changed(by_path)

    by_file = SimpleNamespace(
        type="update", key=["clips", {"id": "C1"}],
        values={"file_id": "F2", "reader": {"path": "/same.mp4"}},
        old_values={"file_id": "F1", "reader": {"path": "/same.mp4"}},
    )
    assert timeline_module.media_source_changed(by_file)

    proxy_gone = SimpleNamespace(
        type="update", key=["clips", {"id": "C1"}],
        values={"file_id": "F1", "reader": {"path": "/a.mp4"}},
        old_values={"file_id": "F1", "reader": {"path": "/a.mp4",
                                                "proxy_reader": {"path": "/proxy.mp4"}}},
    )
    assert timeline_module.media_source_changed(proxy_gone)

    alpha_only = SimpleNamespace(
        type="update", key=["clips", {"id": "C1"}],
        values={"alpha": {"Points": []}},
        old_values={"alpha": {"Points": []}, "file_id": "F1", "reader": {"path": "/a.mp4"}},
    )
    assert not timeline_module.media_source_changed(alpha_only)

    assert not timeline_module.media_source_changed(
        SimpleNamespace(type="insert", key=["clips"], values={}, old_values=None))


def test_the_edited_frame_still_updates_without_a_full_clear(tmp_path):
    """Real libopenshot: a property diff alone makes the next GetFrame show the change."""
    import json
    import os
    import openshot
    QtGui = pytest.importorskip("PyQt5.QtGui")
    if not hasattr(openshot, "Timeline") or os.environ.get("ZENVI_REAL_QT") != "1":
        pytest.skip("needs real libopenshot (ZENVI_REAL_QT=1)")
    image = QtGui.QImage(64, 36, QtGui.QImage.Format_RGB32)
    image.fill(QtGui.QColor("#ffffff"))
    path = str(tmp_path / "white.png")
    image.save(path)

    clip = json.loads(openshot.Clip(path).Json())
    clip.update({"id": "C1", "layer": 1, "position": 0.0, "end": 2.0})
    timeline = openshot.Timeline(64, 36, openshot.Fraction(25, 1), 48000, 2, openshot.LAYOUT_STEREO)
    timeline.SetJson(json.dumps({"clips": [clip], "effects": [], "duration": 2.0}))
    timeline.Open()

    def is_white():
        out = str(tmp_path / "frame.png")
        timeline.GetFrame(10).Save(out, 1.0)
        return QtGui.QColor(QtGui.QImage(out).pixel(32, 18)).value() > 200

    assert is_white()  # and frame 10 is now cached

    sync = timeline_module.TimelineSync.__new__(timeline_module.TimelineSync)
    sync.timeline = timeline
    sync.timeline_lock = MagicMock()
    sync.window = SimpleNamespace(
        preview_thread=SimpleNamespace(player=SimpleNamespace(Mode=lambda: openshot.PLAYBACK_PLAY)),
        proxy_service=None, refreshFrameSignal=MagicMock())
    clip["alpha"] = {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2}]}
    diff = [{"type": "update", "key": ["clips", {"id": "C1"}], "value": clip, "partial": False}]
    sync.changed(SimpleNamespace(type="update", key=["clips", {"id": "C1"}],
                                 json=lambda **k: json.dumps(diff)))

    assert not is_white()  # the transparent clip is gone from the cached frame


def test_an_edit_rearms_background_cache_warming(monkeypatch):
    sync = _sync(monkeypatch)
    warmer = MagicMock()
    sync.window.background_render_manager = warmer
    sync.changed(SimpleNamespace(
        type="update", key=["clips", {"id": "C1"}],
        values={"id": "C1"}, old_values={"id": "C1"}, json=lambda **k: "[]"))
    warmer.invalidate.assert_called_once()


def test_a_batched_media_relink_clears_the_preview_cache_once(monkeypatch):
    from classes import update_batch

    sync = _sync(monkeypatch)
    sync._batched_diffs = []
    sync._batched_property_edit = False
    sync._batched_media_source_changed = False
    update_batch.register(sync.flush_batch)

    def _json(is_array=False, only_value=False, **_):
        payload = {
            "type": "update",
            "key": ["clips", {"id": "C1"}],
            "value": {"id": "C1", "file_id": "F2", "reader": {"path": "/v2.mp4"}},
            "old_values": {"id": "C1", "file_id": "F1", "reader": {"path": "/v1.mp4"}},
        }
        import json
        return json.dumps([payload] if is_array else payload)

    with update_batch.batch():
        sync.changed(SimpleNamespace(
            type="update", key=["clips", {"id": "C1"}],
            values={"id": "C1", "file_id": "F2", "reader": {"path": "/v2.mp4"}},
            old_values={"id": "C1", "file_id": "F1", "reader": {"path": "/v1.mp4"}},
            json=_json,
        ))
        sync.timeline.ApplyJsonDiff.assert_not_called()

    sync.timeline.ApplyJsonDiff.assert_called_once()
    sync.timeline.ClearAllCache.assert_called_once_with(True)
    sync.window.refreshFrameSignal.emit.assert_called_once()
