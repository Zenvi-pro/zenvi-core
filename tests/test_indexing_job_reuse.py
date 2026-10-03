"""Indexing reuses a finished analysis of the same content instead of paying again.

Drives ``IndexingJob`` (the worker's Qt-free logic), so this runs in the ordinary headless
suite and in CI. ``test_indexing_worker_charge.py`` covers the QThread wrapper around it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import credits_client as cc  # noqa: E402
from classes.api_client import ZenviBackendClient  # noqa: E402
from classes.media_index import Shelf  # noqa: E402
from classes.media_index.job import IndexingJob  # noqa: E402
from classes.media_fingerprint import fingerprint  # noqa: E402

ANALYSIS = {
    "analyzed": True, "provider": "gemini-flash-lite", "short_summary": "a street at night",
    "chapters": [{"start": 0.0, "end": 5.0, "title": "Street", "summary": "cars"}],
    "transcript_cues": [], "moments": [],
}


def _ready_result(file_id, project="p1"):
    return {"success": True, "index_id": f"zenvi-{project}", "video_id": file_id,
            "ai_metadata": dict(ANALYSIS)}


class Harness:
    def __init__(self, monkeypatch, tmp_path, *, signed_in=True, restore=None, index_result=None):
        self.monkeypatch = monkeypatch
        self.shelf = Shelf(str(tmp_path / "shelf"))
        monkeypatch.setattr("classes.media_index.store.default_shelf", lambda: self.shelf)
        monkeypatch.setattr("classes.media_index.default_shelf", lambda: self.shelf)
        self.charges = []
        monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (True, 1000, None))
        monkeypatch.setattr(cc, "charge_operation_on_success", lambda *a, **k: self.charges.append((a, k)))
        monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: signed_in))
        self.client = MagicMock()
        self.client._empty_ai_metadata.side_effect = ZenviBackendClient._empty_ai_metadata
        self.client.is_indexing_configured.return_value = True
        self.client.start_direct_indexing_job.side_effect = (
            lambda *a, **k: index_result(k) if callable(index_result) else (index_result or _ready_result(k.get("file_id"))))
        self.client.restore_index.return_value = restore if restore is not None else {"success": True}
        self.media = tmp_path / "clip.mp4"
        self.media.write_bytes(os.urandom(4096))
        self.sha = fingerprint(str(self.media))["sha256"]

    def run(self, *, file_id="f1", project="p1", duration=30.0, media_type="video", summarize_only=False,
            existing_ai=None, with_fingerprint=True, path=None):
        data = {"id": file_id, "path": path or str(self.media), "media_type": media_type, "duration": duration}
        if with_fingerprint:
            data["fingerprint"] = fingerprint(data["path"])
        if existing_ai is not None:
            data["ai_metadata"] = existing_ai
        done, progress = [], []
        IndexingJob(
            data, project, summarize_only,
            client_factory=lambda: self.client,
            emit_completed=lambda fd, md, err: done.append((md, err)),
            emit_progress=lambda fid, phase, pct: progress.append((phase, pct)),
            emit_intermediate=lambda fid, md: None,
        ).run()
        assert len(done) == 1, "the job must report exactly once"
        return done[0][0], progress


def test_a_new_clip_is_indexed_charged_once_and_kept_on_the_shelf(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    meta, _ = h.run()

    h.client.start_direct_indexing_job.assert_called_once()
    h.client.restore_index.assert_not_called()
    assert len(h.charges) == 1
    assert meta["index"]["status"] == "ready" and meta["index"]["video_id"] == "f1"
    saved = h.shelf.load_v1_index(h.sha)
    assert saved["ai_metadata"]["short_summary"] == "a street at night"
    assert saved["source"] == {"duration": 30.0, "media_type": "video", "project_id": "p1", "file_id": "f1"}


def test_the_same_video_in_another_project_is_restored_not_reindexed_or_charged(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.run(file_id="f1", project="p1")
    h.client.start_direct_indexing_job.reset_mock()
    h.charges.clear()

    meta, progress = h.run(file_id="f2", project="p2")

    h.client.start_direct_indexing_job.assert_not_called()
    h.client.restore_index.assert_called_once()
    args, kwargs = h.client.restore_index.call_args
    assert kwargs["file_id"] == "f2" and kwargs["project_id"] == "p2"
    assert kwargs["index_name"] == "zenvi-p2" and kwargs["duration_sec"] == 30.0
    assert args[0]["index"]["video_id"] == "f2", "the backend is told the new ids"
    assert h.charges == [], "a restore costs the user nothing"
    assert meta["index"]["video_id"] == "f2" and meta["index"]["index_id"] == "zenvi-p2"
    assert meta["index"]["restored"] is True
    assert meta["short_summary"] == "a street at night"
    assert ("done", 100) in progress


def test_a_renamed_or_moved_copy_is_recognised_by_its_content(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.run(file_id="f1")
    moved = tmp_path / "elsewhere" / "renamed.mov"
    moved.parent.mkdir()
    moved.write_bytes(h.media.read_bytes())

    h.client.start_direct_indexing_job.reset_mock()
    h.run(file_id="f9", path=str(moved))
    h.client.start_direct_indexing_job.assert_not_called()
    h.client.restore_index.assert_called_once()


def test_a_backend_without_the_restore_route_falls_back_to_normal_indexing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, restore={"success": False, "unsupported": True, "error": "no route"})
    h.run(file_id="f1")
    h.charges.clear()
    h.client.start_direct_indexing_job.reset_mock()

    meta, _ = h.run(file_id="f2", project="p2")
    h.client.start_direct_indexing_job.assert_called_once()
    assert len(h.charges) == 1
    assert meta["index"]["video_id"] == "f2" and not meta["index"].get("restored")


def test_a_failed_restore_falls_back_to_normal_indexing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, restore={"success": False, "unsupported": False, "error": "db down"})
    h.run(file_id="f1")
    h.client.start_direct_indexing_job.reset_mock()
    h.run(file_id="f2", project="p2")
    h.client.start_direct_indexing_job.assert_called_once()


def test_a_restore_that_raises_never_breaks_indexing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.run(file_id="f1")
    h.client.restore_index.side_effect = RuntimeError("boom")
    h.client.start_direct_indexing_job.reset_mock()
    meta, _ = h.run(file_id="f2", project="p2")
    h.client.start_direct_indexing_job.assert_called_once()
    assert meta["index"]["status"] == "ready"


def test_a_file_with_another_duration_is_not_mistaken_for_the_saved_one(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.run(file_id="f1", duration=30.0)
    h.client.start_direct_indexing_job.reset_mock()
    h.run(file_id="f2", project="p2", duration=95.0)
    h.client.restore_index.assert_not_called()
    h.client.start_direct_indexing_job.assert_called_once()


def test_an_explicit_reindex_never_reuses_the_saved_analysis(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.run(file_id="f1")
    h.client.start_direct_indexing_job.reset_mock()
    h.run(file_id="f2", project="p2", summarize_only=True)
    h.client.restore_index.assert_not_called()
    h.client.start_direct_indexing_job.assert_called_once()


def test_a_failed_index_is_not_saved_and_not_charged(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, index_result={"error": "Gemini upload failed"})
    h.run()
    assert h.charges == []
    assert h.shelf.load_v1_index(h.sha) is None
    assert not h.shelf.has_entry(h.sha)


def test_a_file_without_a_stamped_fingerprint_is_still_recognised(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.run(file_id="f1", with_fingerprint=False)
    assert h.shelf.load_v1_index(h.sha) is not None, "the worker fingerprints it itself"
    h.client.start_direct_indexing_job.reset_mock()
    h.run(file_id="f2", project="p2", with_fingerprint=False)
    h.client.restore_index.assert_called_once()


def test_an_analysis_made_before_the_shelf_existed_is_saved_for_other_projects(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    ready = dict(ANALYSIS, index={"status": "ready", "index_id": "zenvi-p1", "video_id": "f1"})
    meta, _ = h.run(file_id="f1", existing_ai=ready)

    h.client.start_direct_indexing_job.assert_not_called()
    assert h.charges == []
    assert meta["analyzed"] is True
    assert h.shelf.load_v1_index(h.sha)["ai_metadata"]["short_summary"] == "a street at night"

    # ...so a second project reuses it.
    h.run(file_id="f2", project="p2")
    h.client.restore_index.assert_called_once()
    h.client.start_direct_indexing_job.assert_not_called()


# -- signed out -----------------------------------------------------------------------
def test_signed_out_skips_cloud_indexing_with_a_sign_in_status_and_costs_nothing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    meta, _ = h.run()

    assert meta["skip_code"] == "signin" and "Sign in" in meta["skip_reason"]
    assert not meta.get("error")
    h.client.start_direct_indexing_job.assert_not_called()
    h.client.restore_index.assert_not_called()
    assert h.charges == []
    from classes.indexing_status import SKIPPED, derive_indexing_status
    status = derive_indexing_status(meta)
    assert status.state == SKIPPED and status.label == "Sign in to index"


def test_signed_out_still_serves_files_that_are_already_indexed(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    ready = dict(ANALYSIS, index={"status": "ready", "index_id": "zenvi-p1", "video_id": "f1"})
    meta, _ = h.run(file_id="f1", existing_ai=ready)
    assert meta["index"]["status"] == "ready" and "skip_code" not in meta
    h.client.start_direct_indexing_job.assert_not_called()


def test_signing_in_later_lets_the_same_file_index(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    skipped, _ = h.run(file_id="f1")
    assert skipped["skip_code"] == "signin"
    monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: True))
    meta, _ = h.run(file_id="f1", existing_ai=skipped)
    h.client.start_direct_indexing_job.assert_called_once()
    assert meta["index"]["status"] == "ready" and "skip_code" not in meta
