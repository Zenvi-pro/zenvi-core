"""Indexing an imported clip is charged exactly once (issue #180).

The backend's /indexing routes do not bill, so this desktop charge is the only
one an import makes. Real-Qt test: BackendIndexingWorker subclasses QThread at
import time.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt5.QtCore")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication
files_model = importlib.import_module("windows.models.files_model")

from classes import credits_client as cc  # noqa: E402
from classes.api_client import ZenviBackendClient  # noqa: E402

READY = {"success": True, "index_id": "zenvi-p1", "video_id": "f1",
         "ai_metadata": {"analyzed": True, "short_summary": "a street at night"}}


def _index(monkeypatch, *, result=READY, ai_metadata=None, blocked=None):
    """Run one BackendIndexingWorker synchronously; return (charges, client)."""
    charges = []
    monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (blocked is None, 1000, blocked))
    monkeypatch.setattr(cc, "charge_operation_on_success", lambda *a, **k: charges.append((a, k)))
    client = MagicMock()
    client._empty_ai_metadata.side_effect = ZenviBackendClient._empty_ai_metadata
    client.is_indexing_configured.return_value = True
    client.start_direct_indexing_job.return_value = result
    monkeypatch.setattr(files_model, "get_backend_client", lambda: client)

    data = {"id": "f1", "path": "/media/clip.mp4", "media_type": "video", "duration": 6.0}
    if ai_metadata is not None:
        data["ai_metadata"] = ai_metadata
    files_model.BackendIndexingWorker(data, project_id="p1").run()
    return charges, client


def test_a_new_clip_is_charged_once_for_its_duration(monkeypatch):
    charges, client = _index(monkeypatch)

    client.start_direct_indexing_job.assert_called_once()
    assert [(a[1], k.get("duration_seconds"), k.get("provider")) for a, k in charges] == [
        ("indexing_per_minute", 6.0, "gemini"),
    ]


def test_an_indexed_clip_is_not_indexed_or_charged_again(monkeypatch):
    ready = {"index": {"status": "ready", "index_id": "zenvi-p1", "video_id": "f1"}}
    charges, client = _index(monkeypatch, ai_metadata=ready)

    client.start_direct_indexing_job.assert_not_called()
    assert charges == []


def test_a_failed_index_is_not_charged(monkeypatch):
    charges, _ = _index(monkeypatch, result={"error": "Gemini upload failed"})
    assert charges == []


def test_a_blocked_preflight_neither_indexes_nor_charges(monkeypatch):
    charges, client = _index(monkeypatch, blocked="Insufficient credits")

    client.start_direct_indexing_job.assert_not_called()
    assert charges == []
