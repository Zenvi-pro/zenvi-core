"""Indexing reuses a finished analysis of the same content instead of doing it again.

Drives ``IndexingJob`` (the worker's Qt-free logic), so this runs in the ordinary headless
suite and in CI. ``test_indexing_worker_charge.py`` covers the QThread wrapper around it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import credits_client as cc  # noqa: E402
from classes.api_client import ZenviBackendClient  # noqa: E402
from classes.media_index import Shelf  # noqa: E402
from classes.media_index.job import IndexingJob  # noqa: E402
from classes.media_fingerprint import fingerprint  # noqa: E402

class Harness:
    def __init__(self, monkeypatch, tmp_path, *, signed_in=True):
        self.monkeypatch = monkeypatch
        self.shelf = Shelf(str(tmp_path / "shelf"))
        monkeypatch.setattr("classes.media_index.store.default_shelf", lambda: self.shelf)
        monkeypatch.setattr("classes.media_index.default_shelf", lambda: self.shelf)
        self.charges = []
        monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (True, 1000, None))
        monkeypatch.setattr(cc, "charge_operation_on_success", lambda *a, **k: self.charges.append((a, k)))
        monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: signed_in))
        monkeypatch.setattr("classes.media_index.facts.compute_facts", lambda *a, **k: {"ok": True})
        self.client = MagicMock()
        self.client._empty_ai_metadata.side_effect = ZenviBackendClient._empty_ai_metadata
        self.media = tmp_path / "clip.mp4"
        self.media.write_bytes(os.urandom(4096))
        self.sha = fingerprint(str(self.media))["sha256"]

    def run(self, *, file_id="f1", project="p1", duration=30.0, media_type="video",
            with_fingerprint=True, path=None, extra=None):
        data = {"id": file_id, "path": path or str(self.media), "media_type": media_type, "duration": duration, **(extra or {})}
        if with_fingerprint:
            data["fingerprint"] = fingerprint(data["path"])
        done, progress = [], []
        IndexingJob(
            data, project,
            client_factory=lambda: self.client,
            emit_completed=lambda fd, md, err: done.append((md, err)),
            emit_progress=lambda fid, phase, pct: progress.append((phase, pct)),
        ).run()
        assert len(done) == 1, "the job must report exactly once"
        return done[0][0], progress


def _stub_vectors(monkeypatch, h):
    """The search vectors: a plain success that writes the layer, so these tests are about the job, not the model."""
    from classes.media_index import embedder as E, vectors
    calls = []

    def fake(path, probe, sha, shelf, embedder, **kw):
        calls.append(sha)
        vectors.write_layer(shelf, sha, [{"kind": "image", "shot": 0, "t": 1.0}], [np.ones(4, np.float32) / 2], model=embedder.model)
        return {"status": "ready", "rows": 1}

    monkeypatch.setattr(vectors, "compute_vectors", fake)
    monkeypatch.setattr(E, "get_embedder", lambda model: type("Emb", (), {"model": model})())
    monkeypatch.setattr(E, "_setting", lambda: "")
    monkeypatch.setattr(E, "describe_on_import", lambda: False)
    monkeypatch.setattr("classes.media_index.probe.probe_media", lambda p: {"has_video": True, "duration": 6.0})
    h.vector_calls = calls
    return calls


# -- local analysis (media-index-v2) --------------------------------------------------
def _facts_calls(monkeypatch, h, raises=None):
    from classes.media_index import facts
    calls = []
    _stub_vectors(monkeypatch, h)

    def fake(path, **kw):
        calls.append((path, kw))
        if raises:
            raise raises
        kw["on_progress"](0.5)
        return {"ok": True}

    monkeypatch.setattr(facts, "compute_facts", fake)
    return calls


def test_the_local_analysis_runs_first_and_reports_progress(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    calls = _facts_calls(monkeypatch, h)
    _meta, progress = h.run()
    assert len(calls) == 1 and calls[0][0] == str(h.media) and calls[0][1]["media_type"] == "video"
    assert progress[0] == ("analyzing", 0) and ("analyzing", 50) in progress


def test_signed_out_the_file_is_still_fully_indexed_and_costs_nothing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    calls = _facts_calls(monkeypatch, h)
    meta, _ = h.run()
    assert len(calls) == 1 and meta["index"]["status"] == "ready" and not meta.get("skip_code") and h.charges == []


def test_a_failing_analysis_never_stops_indexing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    _facts_calls(monkeypatch, h, raises=RuntimeError("decoder exploded"))
    meta, _ = h.run()
    assert meta["index"]["status"] == "ready"


def test_a_long_clip_is_indexed_without_asking(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    calls = _facts_calls(monkeypatch, h)
    meta, _ = h.run(duration=45 * 60.0)
    assert len(calls) == 1 and meta["index"]["status"] == "ready" and not meta.get("skip_code")


def test_cancelling_during_analysis_ends_the_job_without_embedding(monkeypatch, tmp_path):
    from classes.media_index import facts, vectors
    h = Harness(monkeypatch, tmp_path)
    monkeypatch.setattr(vectors, "compute_vectors", lambda *a, **k: pytest.fail("no embedding after a cancel"))
    done, seen = [], []
    job = IndexingJob({"id": "f1", "path": str(h.media), "media_type": "video", "duration": 5.0}, "p1",
                      client_factory=lambda: h.client, emit_completed=lambda fd, md, err: done.append(md),
                      emit_progress=lambda *a: None)

    def fake(path, should_cancel=None, **kw):
        job.cancel()
        seen.append(should_cancel() is True)
        raise facts.Cancelled()

    monkeypatch.setattr(facts, "compute_facts", fake)
    job.run()
    assert seen == [True] and len(done) == 1


def test_a_missing_network_while_setting_up_search_leaves_the_file_queued(monkeypatch, tmp_path):
    """The first search-model download raises URLError offline. That must not fail the file or escape the job."""
    import urllib.error
    from classes.media_index import vectors
    h = Harness(monkeypatch, tmp_path)
    _stub_vectors(monkeypatch, h)

    def offline(*a, **k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(vectors, "compute_vectors", offline)
    done = []
    IndexingJob(
        {"id": "f1", "path": str(h.media), "media_type": "video", "duration": 5.0, "fingerprint": fingerprint(str(h.media))},
        "p1", client_factory=lambda: h.client, emit_completed=lambda fd, md, err: done.append((md, err)),
        emit_progress=lambda *a: None,
    ).run()
    meta, err = done[0]
    assert err is None
    assert not meta.get("error")
    assert (meta.get("index") or {}).get("status") != "failed"
    assert meta.get("retry_index") is True


def test_a_failed_embedding_is_reported_and_out_of_credits_is_a_skip(monkeypatch, tmp_path):
    from classes.media_index import embedder as E, vectors
    h = Harness(monkeypatch, tmp_path)
    _stub_vectors(monkeypatch, h)

    def boom(*a, **k):
        raise E.EmbedError("worker crashed")
    monkeypatch.setattr(vectors, "compute_vectors", boom)
    meta, _ = h.run()
    assert meta["index"]["status"] == "failed" and "worker crashed" in meta["error"]

    def broke(*a, **k):
        raise E.EmbedError("Out of credits", credits=True)
    monkeypatch.setattr(vectors, "compute_vectors", broke)
    meta, _ = h.run(file_id="f2")
    assert meta["index"]["status"] == "skipped" and not meta.get("error")


# -- one indexer per fingerprint: the same footage is never analysed twice at once -----------
def test_two_jobs_for_the_same_footage_embed_it_once(monkeypatch, tmp_path):
    import threading
    import time
    from classes.media_index import job as jobmod, vectors
    h = Harness(monkeypatch, tmp_path)
    calls = _stub_vectors(monkeypatch, h)
    inner = vectors.compute_vectors
    gate, inside = threading.Event(), threading.Event()

    def slow(*a, **k):
        inside.set()
        gate.wait(10)
        return inner(*a, **k)

    monkeypatch.setattr(vectors, "compute_vectors", slow)
    out = {}

    def run(name, file_id, project):
        out[name] = h.run(file_id=file_id, project=project, duration=6.0)[0]

    first = threading.Thread(target=run, args=("a", "f1", "p1"))
    first.start()
    assert inside.wait(10)
    second = threading.Thread(target=run, args=("b", "f2", "p2"))
    second.start()
    deadline = time.time() + 10
    while time.time() < deadline and jobmod._FLIGHTS.get(h.sha, (None, 0))[1] < 2:
        time.sleep(0.01)
    assert jobmod._FLIGHTS[h.sha][1] == 2, "the second job is waiting for the first"
    gate.set()
    first.join(10)
    second.join(10)
    assert len(calls) == 1, "one embedding for the same content"
    assert out["a"]["index"]["video_id"] == "f1" and out["b"]["index"]["video_id"] == "f2"
    assert out["b"]["index"]["index_id"] == "zenvi-p2", "the second job reused the first one's result under its own ids"
    assert jobmod._FLIGHTS == {}, "no turn is left behind"


def test_footage_with_different_content_does_not_wait_for_each_other():
    import threading
    from classes.media_index.job import single_flight
    order = []
    release = threading.Event()

    def hold(key):
        with single_flight(key) as turn:
            order.append((key, turn))
            release.wait(5)

    t = threading.Thread(target=hold, args=("aaa",))
    t.start()
    while not order:
        pass
    with single_flight("bbb") as turn:
        assert turn is True, "another fingerprint is free to go at once"
    release.set()
    t.join(5)


def test_a_job_cancelled_while_waiting_for_its_turn_gives_up_and_leaves_no_trace():
    import threading
    from classes.media_index import job as jobmod
    started, release = threading.Event(), threading.Event()

    def hold():
        with jobmod.single_flight("ccc"):
            started.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert started.wait(5)
    with jobmod.single_flight("ccc", cancelled=lambda: True, poll=0.01) as turn:
        assert turn is False
    release.set()
    t.join(5)
    assert jobmod._FLIGHTS == {}


def test_when_it_cannot_be_told_whether_a_user_is_signed_in_the_answer_is_no(monkeypatch):
    from classes import auth_manager

    class Broken:
        @staticmethod
        def instance():
            raise RuntimeError("keychain locked")

    monkeypatch.setattr(auth_manager, "AuthManager", Broken)
    assert IndexingJob._signed_in() is False


def test_a_signed_in_user_is_signed_in(monkeypatch):
    from classes import auth_manager
    monkeypatch.setattr(auth_manager, "AuthManager", type("A", (), {"instance": staticmethod(lambda: MagicMock(is_authenticated=lambda: True))}))
    assert IndexingJob._signed_in() is True
