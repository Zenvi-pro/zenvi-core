"""An edit must not throw away every decoded source frame (#267, #320).

ClearAllCache(True) also emptied each clip's nested FFmpegReader cache, so a
single property tweak forced every clip (4K included) to decode again while the
GUI thread held the timeline lock.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("openshot")

from classes import timeline as timeline_module  # noqa: E402


def test_property_edit_keeps_decoded_source_frames(monkeypatch):
    settings = SimpleNamespace(ENABLE_PLAYBACK_CACHING=True)
    monkeypatch.setattr(timeline_module.openshot, "Settings",
                        SimpleNamespace(Instance=lambda: settings), raising=False)
    monkeypatch.setattr(timeline_module.openshot, "PLAYBACK_PLAY", 1, raising=False)
    sync = timeline_module.TimelineSync.__new__(timeline_module.TimelineSync)
    sync.timeline = MagicMock()
    sync.timeline_lock = MagicMock()
    sync.window = SimpleNamespace(
        preview_thread=SimpleNamespace(player=SimpleNamespace(Mode=lambda: 0)),
        proxy_service=None,
        refreshFrameSignal=MagicMock(),
    )
    action = SimpleNamespace(type="update", key=["clips", {"id": "C1"}], json=lambda **k: "[]")

    sync.changed(action)

    sync.timeline.ApplyJsonDiff.assert_called_once()
    sync.timeline.ClearAllCache.assert_called_once_with(False)
    sync.window.refreshFrameSignal.emit.assert_called_once()
