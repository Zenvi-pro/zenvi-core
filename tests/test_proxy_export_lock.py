"""Export must never resolve to a proxy path."""

from __future__ import annotations

import os
from types import SimpleNamespace

from classes.proxy_service import dialog_preview_reader_data, resolve_reader


def test_for_export_ignores_proxy_reader(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    proxy = tmp_path / "proxy_clip.mp4"
    source.write_bytes(b"src")
    proxy.write_bytes(b"proxy")

    monkeypatch.setattr(
        "classes.proxy_service.absolute_media_path",
        lambda p: str(p) if p else "",
    )
    file_obj = SimpleNamespace(
        id="F1",
        data={
            "id": "F1",
            "path": str(source),
            "proxy_reader": {"path": str(proxy), "width": 640},
        },
    )
    exported = resolve_reader(file_obj, for_export=True)
    assert exported["path"] == str(source)
    assert "proxy" not in os.path.basename(exported["path"]).lower() or exported["path"] == str(source)
    assert exported["path"] != str(proxy)

    preview = resolve_reader(file_obj, for_export=False)
    assert preview["path"] == str(proxy)


def test_missing_proxy_falls_back_to_source(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"src")
    monkeypatch.setattr(
        "classes.proxy_service.absolute_media_path",
        lambda p: str(p) if p else "",
    )
    file_obj = SimpleNamespace(
        id="F1",
        data={
            "path": str(source),
            "proxy_reader": {"path": str(tmp_path / "gone.mp4")},
        },
    )
    preview = resolve_reader(file_obj, for_export=False)
    assert preview["path"] == str(source)
    exported = resolve_reader(file_obj, for_export=True)
    assert exported["path"] == str(source)


def test_dialog_preview_helper_matches_resolve_reader(tmp_path, monkeypatch):
    source = tmp_path / "a.mov"
    source.write_bytes(b"x")
    monkeypatch.setattr(
        "classes.proxy_service.absolute_media_path",
        lambda p: str(p) if p else "",
    )
    file_obj = SimpleNamespace(id="F", data={"path": str(source)})
    assert dialog_preview_reader_data(file_obj, prefer_proxy=False)["path"] == resolve_reader(
        file_obj, for_export=True
    )["path"]
