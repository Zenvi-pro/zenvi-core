"""Importing several files keeps the selection / drop order instead of
re-sorting by filename (OpenShot #6051). Real-Qt test: the files model
subclasses QThread at import time."""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtCore")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication
files_model_module = importlib.import_module("windows.models.files_model")


def _fake_app():
    return types.SimpleNamespace(
        updates=types.SimpleNamespace(transaction_id=None),
        window=types.SimpleNamespace(OpenProjectSignal=types.SimpleNamespace(emit=lambda *a, **k: None)),
    )


def _run_process_urls(monkeypatch, paths, *, isdir=lambda p: False, walk=None):
    captured = {}

    def record_add_files(media_paths, quiet=False, prevent_image_seq=False):
        captured["paths"] = list(media_paths)
        captured["quiet"] = quiet
        return [types.SimpleNamespace(id=f"id-{i}") for i, _ in enumerate(media_paths)]

    fake_self = types.SimpleNamespace(add_files=record_add_files)
    monkeypatch.setattr(files_model_module, "get_app", _fake_app)
    monkeypatch.setattr(files_model_module, "local_path_from_url", lambda uri: uri)
    monkeypatch.setattr(files_model_module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(files_model_module.os.path, "isdir", isdir)
    monkeypatch.setattr(files_model_module.os.path, "isfile", lambda p: not isdir(p))
    if walk is not None:
        monkeypatch.setattr(files_model_module.os, "walk", walk)
    import classes.updates as updates_module
    monkeypatch.setattr(updates_module, "nested_transaction", lambda _updates: contextlib.nullcontext())

    result = files_model_module.FilesModel.process_urls(fake_self, paths)
    return captured, result


def test_process_urls_preserves_original_url_order_for_import(monkeypatch):
    captured, result = _run_process_urls(monkeypatch, ["/tmp/zeta.mp4", "/tmp/alpha.mp4", "/tmp/mid.mp4"])

    assert captured["paths"] == ["/tmp/zeta.mp4", "/tmp/alpha.mp4", "/tmp/mid.mp4"]
    assert len(result) == 3


def test_process_urls_walks_folders_in_a_stable_sorted_order(monkeypatch):
    def fake_walk(root):
        dirs = ["b", "a"]
        yield root, dirs, ["z.mp4", "a.mp4"]
        assert dirs == ["a", "b"]  # the walk order itself was made deterministic
        for d in dirs:
            yield f"{root}/{d}", [], ["2.png", "1.png"]

    captured, _ = _run_process_urls(
        monkeypatch, ["/media/folder"], isdir=lambda p: p == "/media/folder", walk=fake_walk)

    assert captured["quiet"] is True
    assert captured["paths"] == [
        "/media/folder/a.mp4", "/media/folder/z.mp4",
        "/media/folder/a/1.png", "/media/folder/a/2.png",
        "/media/folder/b/1.png", "/media/folder/b/2.png",
    ]
