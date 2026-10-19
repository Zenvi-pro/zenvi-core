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


# -- signed out -----------------------------------------------------------------------
def test_signed_out_skips_cloud_indexing_with_a_sign_in_status_and_costs_nothing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    meta, _ = h.run()

    assert meta["skip_code"] == "signin" and "Sign in" in meta["skip_reason"]
    assert not meta.get("error")
    assert h.charges == []
    from classes.indexing_status import SKIPPED, derive_indexing_status
    status = derive_indexing_status(meta)
    assert status.state == SKIPPED and status.label == "Sign in to index"


# -- local analysis (media-index-v2) --------------------------------------------------
def _facts_calls(monkeypatch, raises=None):
    from classes.media_index import cloud, facts
    calls = []
    # These tests are about the local analysis; the cloud step is a plain success here.
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: {"layers": {"watch": "ready", "vectors": "ready"}})

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
    calls = _facts_calls(monkeypatch)
    _meta, progress = h.run()
    assert len(calls) == 1 and calls[0][0] == str(h.media) and calls[0][1]["media_type"] == "video"
    assert progress[0] == ("analyzing", 0) and ("analyzing", 50) in progress


def test_the_analysis_runs_even_when_signed_out(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    calls = _facts_calls(monkeypatch)
    meta, _ = h.run()
    assert len(calls) == 1 and meta["skip_code"] == "signin"


def test_a_failing_analysis_never_stops_indexing(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    _facts_calls(monkeypatch, raises=RuntimeError("decoder exploded"))
    meta, _ = h.run()
    assert meta["index"]["status"] == "ready"


def test_a_clip_over_the_cloud_cap_is_still_analysed_locally(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    calls = _facts_calls(monkeypatch)
    meta, _ = h.run(duration=45 * 60.0)
    assert len(calls) == 1 and meta["skip_code"] == "long_file" and "Approve it with index_long_file_tool" in meta["skip_reason"]


def test_cancelling_during_analysis_ends_the_job_without_cloud_work(monkeypatch, tmp_path):
    from classes.media_index import cloud, facts
    h = Harness(monkeypatch, tmp_path)
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: pytest.fail("no cloud work after a cancel"))
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


# -- media index v2: the cloud layers ----------------------------------------------------
V2_WATCH = {"shots": [{"id": 0, "start": 0.0, "end": 6.0, "description": "A calm pond.", "actions": ["ripples"],
                       "objects": [{"label": "pond"}], "sound_events": [], "mood": "calm", "shot_type": "wide"}]}


def _v2(monkeypatch, tmp_path, *, signed_in=True, result=None, populate=False, blocked=None):
    from classes.media_index import cloud
    h = Harness(monkeypatch, tmp_path, signed_in=signed_in)
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
    assert meta["index"]["status"] == "ready" and meta["index"]["v2"] is True and meta["index"]["provider"] == "gemini-v2"
    assert meta["index"]["video_id"] == "f1" and meta["index"]["index_id"] == "zenvi-p1" and meta["index"]["fingerprint"] == h.sha
    assert meta["analyzed"] and meta["chapters"][0]["summary"] == "A calm pond." and meta["twelvelabs"]["status"] == "ready"
    assert h.charges == [], "the backend meters /index/v2 from Gemini's real usage; the desktop charges nothing"
    assert ("uploading", 50) in progress and ("indexing", -1) in progress and progress[-1] == ("done", 100)
    from classes.indexing_status import SUCCESS, derive_indexing_status
    assert derive_indexing_status(meta).state == SUCCESS


def test_with_v2_on_a_video_already_on_the_shelf_costs_nothing_and_needs_no_sign_in(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, signed_in=False, populate=True)
    meta, _ = h.run(file_id="f2", project="p2", duration=6.0)
    assert calls == [] and h.charges == []
    assert meta["index"]["video_id"] == "f2" and meta["index"]["index_id"] == "zenvi-p2" and meta["chapters"]


def test_a_renamed_or_moved_copy_is_recognised_by_its_content(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, populate=True)
    moved = tmp_path / "elsewhere" / "renamed.mov"
    moved.parent.mkdir()
    moved.write_bytes(h.media.read_bytes())
    meta, _ = h.run(file_id="f9", path=str(moved), duration=6.0)
    assert calls == [] and meta["index"]["video_id"] == "f9"


def test_a_file_without_a_stamped_fingerprint_is_still_recognised(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, populate=True)
    meta, _ = h.run(file_id="f1", duration=6.0, with_fingerprint=False)
    assert calls == [] and meta["index"]["video_id"] == "f1", "the worker fingerprints it itself"


def test_signing_in_later_lets_the_same_file_index(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, signed_in=False)
    skipped, _ = h.run(file_id="f1", duration=6.0)
    assert skipped["skip_code"] == "signin" and calls == []
    monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: True))
    meta, _ = h.run(file_id="f1", duration=6.0)
    assert len(calls) == 1 and meta["index"]["status"] == "ready" and "skip_code" not in meta


def test_a_v2_failure_is_reported_and_not_charged(monkeypatch, tmp_path):
    h, _ = _v2(monkeypatch, tmp_path, result={"error": "quota exceeded", "layers": {}})
    meta, _ = h.run(duration=6.0)
    assert meta["index"]["status"] == "failed" and meta["error"] == "quota exceeded" and meta["index"]["provider"] == "gemini-v2"
    assert h.charges == []


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


def test_cancelling_during_v2_ends_the_job_without_a_charge(monkeypatch, tmp_path):
    from classes.media_index import cloud
    h, _ = _v2(monkeypatch, tmp_path)
    done, reached = [], []
    job = IndexingJob({"id": "f1", "path": str(h.media), "media_type": "video", "duration": 6.0}, "p1",
                      client_factory=lambda: h.client, emit_completed=lambda fd, md, err: done.append(md),
                      emit_progress=lambda *a: None)

    def cancelling(*a, **k):
        reached.append(True)
        job.cancel()
        raise cloud.Cancelled()

    monkeypatch.setattr(cloud, "compute_cloud", cancelling)
    job.run()
    assert reached == [True] and len(done) == 1 and h.charges == []


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


def test_the_desktop_never_charges_for_a_v2_run_the_backend_meters_it(monkeypatch, tmp_path):
    (tmp_path / "video").mkdir()
    (tmp_path / "song").mkdir()
    video, _ = _v2(monkeypatch, tmp_path / "video", result={"layers": {"watch": "ready", "vectors": "ready"}})
    video.run(duration=6.0)
    assert video.charges == []
    song, _ = _v2(monkeypatch, tmp_path / "song", result={"layers": {"watch": "skipped", "vectors": "skipped"}, "nothing_to_embed": True})
    song.shelf.write_json(song.sha, "audio.json", {"tempo": None})
    song.shelf.set_layer(song.sha, "audio", version=2, status="ready")
    song.run(media_type="audio", duration=40.0)
    assert song.charges == []


@pytest.mark.parametrize("flag", ["credits", "rate_limited"])
def test_an_account_out_of_credits_or_over_its_limit_is_skipped_with_the_servers_message_not_failed(monkeypatch, tmp_path, flag):
    h, _ = _v2(monkeypatch, tmp_path, result={"error": "Out of credits for video indexing.", flag: True, "layers": {}})
    meta, _ = h.run(duration=6.0)
    assert meta["index"]["status"] == "skipped" and "Out of credits" in meta["index"]["error"] and "error" not in meta
    assert h.charges == []


def test_a_settled_song_is_not_sent_to_the_cloud_again_even_signed_out(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path)
    h.shelf.write_json(h.sha, "audio.json", {"tempo": None})
    h.shelf.set_layer(h.sha, "audio", version=2, status="ready")
    h.shelf.set_layer(h.sha, "vectors", version=1, status="not_applicable", note="nothing to embed", had_speech=False)
    monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: False))
    meta, _ = h.run(media_type="audio", duration=40.0)
    assert calls == [] and h.charges == [] and meta["index"]["status"] == "ready" and meta["analyzed"] is True
    assert meta.get("skip_code") != "signin", "a finished file does not ask to sign in"


# -- one indexer per fingerprint: the same footage is never analysed (or paid for) twice at once -----------
def test_two_jobs_for_the_same_footage_pay_for_one_analysis(monkeypatch, tmp_path):
    import threading
    import time
    from classes.media_index import cloud, job as jobmod
    h, calls = _v2(monkeypatch, tmp_path, result={"layers": {"watch": "ready", "vectors": "ready"}})
    inner = cloud.compute_cloud
    gate, inside = threading.Event(), threading.Event()

    def slow(*a, **k):
        inside.set()
        gate.wait(10)
        return inner(*a, **k)

    monkeypatch.setattr(cloud, "compute_cloud", slow)
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
    assert len(calls) == 1, "one cloud analysis for the same content"
    assert out["a"]["index"]["video_id"] == "f1" and out["b"]["index"]["video_id"] == "f2"
    assert out["b"]["index"]["index_id"] == "zenvi-p2" and out["b"]["chapters"], "the second job reused the first one's result under its own ids"
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
