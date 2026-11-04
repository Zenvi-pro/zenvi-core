"""Video thumbnails are the frame itself, without the film-sprocket overlay that
turned every timeline clip into a striped strip (#330)."""

from __future__ import annotations

import pytest

pytest.importorskip("openshot")

from classes import proxy_service  # noqa: E402


def test_prewarmed_video_thumbnail_has_no_overlay(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(proxy_service, "ThumbnailPathForFrame", lambda *a, **k: str(tmp_path / "t.png"))
    monkeypatch.setattr(proxy_service, "GenerateThumbnailFromFrame", lambda *a, **k: calls.append(a))

    service = proxy_service.ProxyService.__new__(proxy_service.ProxyService)
    service._save_prewarmed_thumbnail(object(), "F1", 1, 0)

    assert calls and calls[0][5] == ""
