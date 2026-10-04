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


# -- local analysis (media-index-v2) --------------------------------------------------
def _facts_calls(monkeypatch, enabled, raises=None):
    from classes.media_index import cloud, facts, flags
    calls = []
    monkeypatch.setattr(flags, "v2_enabled", lambda: enabled)
    # These tests are about the local analysis; an old backend (no v2 routes) lets the original indexing continue.
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: {"unsupported": True, "error": "no v2", "layers": {}})

    def fake(path, **kw):
        calls.append((path, kw))
        if raises:
            raise raises
        kw["on_progress"](0.5)
        return {"ok": True}

    monkeypatch.setattr(facts, "compute_facts", fake)
    return calls


def test_with_the_preference_off_no_local_analysis_runs(monkeypatch, tmp_path):
    calls = _facts_calls(monkeypatch, enabled=False)
    h = Harness(monkeypatch, tmp_path)
    h.run()
    assert calls == []
    h.client.start_direct_indexing_job.assert_called_once()


def test_with_it_on_the_analysis_runs_first_and_reports_progress(monkeypatch, tmp_path):
    calls = _facts_calls(monkeypatch, enabled=True)
    h = Harness(monkeypatch, tmp_path)
    _meta, progress = h.run()
    assert len(calls) == 1 and calls[0][0] == str(h.media) and calls[0][1]["media_type"] == "video"
    assert progress[0] == ("analyzing", 0) and ("analyzing", 50) in progress
    h.client.start_direct_indexing_job.assert_called_once()


def test_the_analysis_runs_even_when_signed_out(monkeypatch, tmp_path):
    calls = _facts_calls(monkeypatch, enabled=True)
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    meta, _ = h.run()
    assert len(calls) == 1 and meta["skip_code"] == "signin"


def test_a_failing_analysis_never_stops_indexing(monkeypatch, tmp_path):
    _facts_calls(monkeypatch, enabled=True, raises=RuntimeError("decoder exploded"))
    h = Harness(monkeypatch, tmp_path)
    meta, _ = h.run()
    h.client.start_direct_indexing_job.assert_called_once()
    assert meta["index"]["status"] == "ready"


def test_a_clip_over_the_cloud_cap_is_still_analysed_locally(monkeypatch, tmp_path):
    calls = _facts_calls(monkeypatch, enabled=True)
    h = Harness(monkeypatch, tmp_path)
    meta, _ = h.run(duration=45 * 60.0)
    assert len(calls) == 1 and "30-minute" in meta["skip_reason"]
    h.client.start_direct_indexing_job.assert_not_called()


def test_cancelling_during_analysis_ends_the_job_without_cloud_work(monkeypatch, tmp_path):
    from classes.media_index import facts, flags
    monkeypatch.setattr(flags, "v2_enabled", lambda: True)
    h = Harness(monkeypatch, tmp_path)
    done = []
    job = IndexingJob({"id": "f1", "path": str(h.media), "media_type": "video", "duration": 5.0}, "p1", False,
                      client_factory=lambda: h.client, emit_completed=lambda fd, md, err: done.append(md),
                      emit_progress=lambda *a: None, emit_intermediate=lambda *a: None)

    def fake(path, should_cancel=None, **kw):
        job.cancel()
        assert should_cancel() is True
        raise facts.Cancelled()

    monkeypatch.setattr(facts, "compute_facts", fake)
    job.run()
    assert len(done) == 1
    h.client.start_direct_indexing_job.assert_not_called()
    h.client.restore_index.assert_not_called()


# -- media index v2: the cloud layers ----------------------------------------------------
V2_WATCH = {"shots": [{"id": 0, "start": 0.0, "end": 6.0, "description": "A calm pond.", "actions": ["ripples"],
                       "objects": [{"label": "pond"}], "sound_events": [], "mood": "calm", "shot_type": "wide"}]}


def _v2(monkeypatch, tmp_path, *, signed_in=True, result=None, populate=False, blocked=None, charges=None):
    from classes.media_index import cloud, flags
    monkeypatch.setattr(flags, "v2_enabled", lambda: True)
    h = Harness(monkeypatch, tmp_path, signed_in=signed_in)
    h.client.start_direct_indexing_job.reset_mock()
    calls = []

    def fake_cloud(client, path, probe, sha, shelf, **kw):
        calls.append(kw)
        if result and (result.get("layers") or {}).get("watch") == "ready":
            shelf.write_json(sha, "structure.json", {"shots": [{"id": 0, "start": 0.0, "end": 6.0}]})
            shelf.write_json(sha, "watch.json", V2_WATCH)
            for layer in ("watch", "vectors"):
                shelf.set_layer(sha, layer, version=1, status="ready")
        kw["on_progress"](0.2)
        kw["on_progress"](0.7)
        return result if result is not None else {"layers": {"watch": "ready", "vectors": "ready"}}

    monkeypatch.setattr(cloud, "compute_cloud", fake_cloud)
    monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (blocked is None, 1000, blocked))
    from classes.media_index import facts
    monkeypatch.setattr(facts, "compute_facts", lambda *a, **k: {"ok": True})
    if populate:
        h.shelf.write_json(h.sha, "structure.json", {"shots": [{"id": 0, "start": 0.0, "end": 6.0}]})
        h.shelf.write_json(h.sha, "watch.json", V2_WATCH)
        for layer in ("watch", "vectors"):
            h.shelf.set_layer(h.sha, layer, version=1, status="ready")
    return h, calls


def test_with_v2_on_a_new_video_gets_the_cloud_layers_and_v1_shaped_metadata(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, result={"layers": {"watch": "ready", "vectors": "ready"}})
    meta, progress = h.run(duration=6.0)
    assert len(calls) == 1 and calls[0]["file_id"] == "f1" and calls[0]["media_type"] == "video"
    h.client.start_direct_indexing_job.assert_not_called()
    assert meta["index"]["status"] == "ready" and meta["index"]["v2"] is True and meta["index"]["provider"] == "gemini-v2"
    assert meta["index"]["video_id"] == "f1" and meta["index"]["index_id"] == "zenvi-p1" and meta["index"]["fingerprint"] == h.sha
    assert meta["analyzed"] and meta["chapters"][0]["summary"] == "A calm pond." and meta["twelvelabs"]["status"] == "ready"
    assert len(h.charges) == 1 and h.charges[0][1]["provider"] == "gemini"
    assert ("uploading", 50) in progress and ("indexing", -1) in progress and progress[-1] == ("done", 100)
    from classes.indexing_status import SUCCESS, derive_indexing_status
    assert derive_indexing_status(meta).state == SUCCESS


def test_with_v2_on_a_video_already_on_the_shelf_costs_nothing_and_needs_no_sign_in(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, signed_in=False, populate=True)
    meta, _ = h.run(file_id="f2", project="p2", duration=6.0)
    assert calls == [] and h.charges == []
    assert meta["index"]["video_id"] == "f2" and meta["index"]["index_id"] == "zenvi-p2" and meta["chapters"]
    h.client.start_direct_indexing_job.assert_not_called()


def test_an_old_backend_without_v2_falls_back_to_the_original_indexing(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, result={"unsupported": True, "error": "no media index v2", "layers": {}})
    meta, _ = h.run(duration=6.0)
    assert len(calls) == 1
    h.client.start_direct_indexing_job.assert_called_once()
    assert meta["index"]["status"] == "ready" and not meta["index"].get("v2")
    assert len(h.charges) == 1


def test_a_v2_failure_is_reported_not_charged_and_not_retried_on_the_old_path(monkeypatch, tmp_path):
    h, _ = _v2(monkeypatch, tmp_path, result={"error": "quota exceeded", "layers": {}})
    meta, _ = h.run(duration=6.0)
    assert meta["index"]["status"] == "failed" and meta["error"] == "quota exceeded" and meta["index"]["provider"] == "gemini-v2"
    assert h.charges == []
    h.client.start_direct_indexing_job.assert_not_called()


def test_signed_out_with_nothing_on_the_shelf_waits_for_sign_in(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, signed_in=False)
    meta, _ = h.run(duration=6.0)
    assert calls == [] and meta["skip_code"] == "signin" and h.charges == []


def test_a_backend_that_rejects_the_token_reads_as_sign_in(monkeypatch, tmp_path):
    h, _ = _v2(monkeypatch, tmp_path, result={"auth": True, "error": "sign in", "layers": {}})
    meta, _ = h.run(duration=6.0)
    assert meta["skip_code"] == "signin" and h.charges == []


def test_blocked_credits_skip_v2_without_calling_the_cloud(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, blocked="Insufficient credits")
    meta, _ = h.run(duration=6.0)
    assert calls == [] and meta["index"]["status"] == "skipped" and "Insufficient" in meta["index"]["error"] and h.charges == []


def test_an_explicit_reindex_does_not_take_the_v2_path(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, populate=True)
    h.run(summarize_only=True, duration=6.0)
    assert calls == []
    h.client.start_direct_indexing_job.assert_called_once()


def test_cancelling_during_v2_ends_the_job_without_a_charge(monkeypatch, tmp_path):
    from classes.media_index import cloud
    h, _ = _v2(monkeypatch, tmp_path)
    done = []
    job = IndexingJob({"id": "f1", "path": str(h.media), "media_type": "video", "duration": 6.0}, "p1", False,
                      client_factory=lambda: h.client, emit_completed=lambda fd, md, err: done.append(md),
                      emit_progress=lambda *a: None, emit_intermediate=lambda *a: None)

    def cancelling(*a, **k):
        job.cancel()
        raise cloud.Cancelled()

    monkeypatch.setattr(cloud, "compute_cloud", cancelling)
    job.run()
    assert len(done) == 1 and h.charges == []
    h.client.start_direct_indexing_job.assert_not_called()


# -- a file with nothing to embed (a song) is finished, uncharged, and marked as analysed music --------------
def test_a_song_with_nothing_to_embed_finishes_uncharged_and_is_marked_analysed_music(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, result={"layers": {"watch": "skipped", "vectors": "skipped"}, "nothing_to_embed": True})
    h.shelf.write_json(h.sha, "audio.json", {"tempo": {"bpm": 110.0, "beats": [1.0, 1.5]}, "loudness": {"integrated_lufs": -14.0},
                                             "music": {"sections": [{"label": "intro"}, {"label": "peak"}]}})
    h.shelf.set_layer(h.sha, "audio", version=2, status="ready")
    meta, progress = h.run(media_type="audio", duration=40.0)
    assert len(calls) == 1 and meta["index"]["status"] == "ready" and meta["index"]["v2"] is True
    assert h.charges == [], "no cloud work ran, so nothing is charged"
    assert meta["analyzed"] is True and meta["short_summary"] == "Audio, 40 s, 110 BPM, sections: intro, peak, -14 LUFS."
    assert progress[-1] == ("done", 100)
    from classes.indexing_status import SUCCESS, derive_indexing_status
    assert derive_indexing_status(meta).state == SUCCESS


def test_a_real_cloud_run_is_charged_but_a_run_that_only_found_nothing_to_embed_is_not(monkeypatch, tmp_path):
    (tmp_path / "video").mkdir()
    (tmp_path / "song").mkdir()
    video, _ = _v2(monkeypatch, tmp_path / "video", result={"layers": {"watch": "ready", "vectors": "ready"}})
    video.run(duration=6.0)
    assert len(video.charges) == 1
    song, _ = _v2(monkeypatch, tmp_path / "song", result={"layers": {"watch": "skipped", "vectors": "skipped"}, "nothing_to_embed": True})
    song.shelf.write_json(song.sha, "audio.json", {"tempo": None})
    song.shelf.set_layer(song.sha, "audio", version=2, status="ready")
    song.run(media_type="audio", duration=40.0)
    assert song.charges == []


def test_a_settled_song_is_not_sent_to_the_cloud_again_even_signed_out(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path)
    h.shelf.write_json(h.sha, "audio.json", {"tempo": None})
    h.shelf.set_layer(h.sha, "audio", version=2, status="ready")
    h.shelf.set_layer(h.sha, "vectors", version=1, status="not_applicable", note="nothing to embed", had_speech=False)
    monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: False))
    meta, _ = h.run(media_type="audio", duration=40.0)
    assert calls == [] and h.charges == [] and meta["index"]["status"] == "ready" and meta["analyzed"] is True
    assert meta.get("skip_code") != "signin", "a finished file does not ask to sign in"
