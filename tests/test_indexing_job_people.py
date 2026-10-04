"""Indexing finds the faces in a video after the local analysis, only when the user turned it on and the models are there."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import flags, library, people, people_models  # noqa: E402
from test_indexing_job_reuse import Harness, _facts_calls  # noqa: E402

READY = {"runtime": True, "models": {"detector": True, "recognizer": True}, "download_bytes": 0, "licenses": {}}


@pytest.fixture
def scanned(monkeypatch):
    calls = []
    monkeypatch.setattr(people, "scan_video", lambda path, sha, shots, duration, **kw: calls.append((path, sha, list(shots), duration)) or {"scan": {}, "assigned": {}})
    monkeypatch.setattr(people, "load_scan", lambda sha: None)
    monkeypatch.setattr(library, "get_file_index", lambda *a, **k: SimpleNamespace(shots=[{"id": 0, "start": 0.0, "end": 5.0}], duration=5.0))
    return calls


def go(monkeypatch, tmp_path, *, people_on=True, status=READY, media_type="video", facts_raise=None):
    _facts_calls(monkeypatch, enabled=True, raises=facts_raise)
    monkeypatch.setattr(flags, "people_enabled", lambda: people_on)
    monkeypatch.setattr(people_models, "status", lambda: status)
    h = Harness(monkeypatch, tmp_path)
    meta, _ = h.run(media_type=media_type)
    return h, meta


def test_with_everything_ready_the_faces_are_found_after_the_local_analysis(monkeypatch, tmp_path, scanned):
    h, meta = go(monkeypatch, tmp_path)
    assert len(scanned) == 1 and scanned[0][0] == str(h.media) and scanned[0][1] == h.sha and scanned[0][2][0]["end"] == 5.0
    h.client.start_direct_indexing_job.assert_called_once()
    assert meta["index"]["status"] == "ready"


@pytest.mark.parametrize("kw", [dict(people_on=False), dict(media_type="audio"),
                                dict(status={**READY, "runtime": False}), dict(status={**READY, "models": {"detector": True, "recognizer": False}})])
def test_nothing_is_scanned_when_it_is_off_not_a_video_or_not_ready_and_nothing_is_downloaded(monkeypatch, tmp_path, scanned, kw):
    monkeypatch.setattr(people_models, "download", lambda *a, **k: pytest.fail("indexing must never download models"))
    h, _ = go(monkeypatch, tmp_path, **kw)
    assert scanned == []
    h.client.start_direct_indexing_job.assert_called_once() if kw.get("media_type") != "audio" else None


def test_a_file_already_scanned_is_not_scanned_again(monkeypatch, tmp_path, scanned):
    monkeypatch.setattr(people, "load_scan", lambda sha: {"tracks": []})
    go(monkeypatch, tmp_path)
    assert scanned == []


def test_a_failing_scan_never_stops_indexing(monkeypatch, tmp_path, scanned):
    monkeypatch.setattr(people, "scan_video", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model exploded")))
    h, meta = go(monkeypatch, tmp_path)
    h.client.start_direct_indexing_job.assert_called_once()
    assert meta["index"]["status"] == "ready"


def test_without_shots_or_after_failed_analysis_there_is_nothing_to_scan(monkeypatch, tmp_path, scanned):
    monkeypatch.setattr(library, "get_file_index", lambda *a, **k: SimpleNamespace(shots=[], duration=5.0))
    go(monkeypatch, tmp_path)
    assert scanned == []
    monkeypatch.setattr(library, "get_file_index", lambda *a, **k: SimpleNamespace(shots=[{"id": 0, "start": 0.0, "end": 5.0}], duration=5.0))
    go(monkeypatch, tmp_path, facts_raise=RuntimeError("no analysis"))
    assert scanned == []
