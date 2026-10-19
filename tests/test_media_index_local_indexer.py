"""The local-first indexer: one embedding space per file, made on this computer (or by the cloud model the user picked),
ranked by each shot's best vector, with Gemini descriptions only on demand."""

from __future__ import annotations

import base64
import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import credits_client as cc  # noqa: E402
from classes.api_client import ZenviBackendClient  # noqa: E402
from classes.media_fingerprint import fingerprint  # noqa: E402
from classes.media_index import cloud, embedder as E, library, search, vectors  # noqa: E402
from classes.media_index import schema as S  # noqa: E402
from classes.media_index.job import IndexingJob  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

DIMS = 16
LOCAL, CLOUD = E.MODEL_LOCAL, E.MODEL_CLOUD


def unit(i):
    v = np.zeros(DIMS, np.float32)
    v[i] = 1.0
    return v


def at(i, cos):
    """A unit vector whose cosine with unit(0) is *cos* (the rest of it points along axis i)."""
    v = unit(0) * cos + unit(i) * float(np.sqrt(max(0.0, 1 - cos * cos)))
    return v / np.linalg.norm(v)


class FakeEmbedder:
    """Stands in for the worker: maps every input to a fixed vector and records what it was asked."""

    def __init__(self, model=LOCAL, vec=None):
        self.model = model
        self.calls = []
        self.vec = unit(1) if vec is None else vec

    def images(self, paths):
        self.calls.append(("images", list(paths)))
        return [self.vec for _ in paths]

    def texts(self, texts, query=False):
        self.calls.append(("texts", list(texts), query))
        return [self.vec for _ in texts]

    def audio(self, windows):
        self.calls.append(("audio", len(windows)))
        return [self.vec for _ in windows]


def put_file(shelf, sha, *, shots, rows, model=LOCAL, sentences=(), watch=None, duration=30.0):
    """One file on the shelf with a vectors_local layer: rows are (row dict, vector)."""
    shelf.set_source(sha, duration=duration, media_type="video", orientation="landscape")
    shelf.write_json(sha, "structure.json", {"shots": shots})
    shelf.set_layer(sha, S.LAYER_STRUCTURE, version=S.LAYER_VERSIONS[S.LAYER_STRUCTURE], status="ready")
    if sentences:
        shelf.write_json(sha, "speech.json", {"sentences": list(sentences), "per_shot": {}})
        shelf.set_layer(sha, S.LAYER_SPEECH, version=S.LAYER_VERSIONS[S.LAYER_SPEECH], status="ready")
    if watch is not None:
        shelf.write_json(sha, "watch.json", {"shots": watch})
        shelf.set_layer(sha, S.LAYER_WATCH, version=S.LAYER_VERSIONS[S.LAYER_WATCH], status="ready")
    if rows is not None:
        vectors.write_layer(shelf, sha, [r for r, _ in rows], [v for _, v in rows], model=model, device="test")


def shot(i, start, end):
    return {"id": i, "start": start, "end": end, "duration": end - start, "motion": {"class": "static", "level": 0.1}, "black": False}


@pytest.fixture
def shelf(tmp_path):
    library.clear_cache()
    return Shelf(str(tmp_path / "shelf"))


A, B = "a" * 64, "b" * 64


def load(shelf, sha, fid):
    return library.get_file_index(shelf, sha, file_id=fid, name=fid)


# ---------------------------------------------------------------- keyframe size
@pytest.mark.parametrize("w,h,want", [(1920, 1080, (528, 288)), (1080, 1920, (288, 528)), (1440, 1080, (432, 336)),
                                      (1080, 1080, (384, 384))])
def test_keyframes_are_cut_at_the_size_the_model_reads_without_padding(w, h, want):
    assert E.keyframe_size(w, h) == want


# ---------------------------------------------------------------- the layer
def test_a_file_becomes_one_set_of_vectors_tagged_with_the_model_that_made_it(shelf, monkeypatch, tmp_path):
    sha = A
    put_file(shelf, sha, shots=[shot(0, 0, 4), shot(1, 4, 10)], rows=None, sentences=[{"start": 1.0, "end": 2.5, "text": "thank you all"}])
    monkeypatch.setattr(vectors, "extract_keyframes", lambda path, plan, size, tmp: [str(tmp_path / f"{i}.jpg") for i in range(len(plan))])
    monkeypatch.setattr(vectors, "audio_windows", lambda path: [(0.0, 10.0, np.zeros(16000, np.float32))])
    emb = FakeEmbedder()
    out = vectors.compute_vectors(str(tmp_path / "clip.mp4"), {"has_video": True, "has_audio": True,
                                                                "video": {"width": 1920, "height": 1080}}, sha, shelf, emb)
    assert out["status"] == "ready"
    assert ("texts", ["thank you all"], False) in emb.calls
    fi = load(shelf, sha, "f1")
    assert fi.vector_model == LOCAL
    assert [r["kind"] for r in fi.text_rows] == ["speech"]
    assert {r["shot"] for r in fi.image_rows} == {0, 1}
    assert len(fi.audio_rows) == 1 and fi.audio_rows[0]["start"] == 0.0
    assert vectors.is_current(shelf, sha, LOCAL) and not vectors.is_current(shelf, sha, CLOUD)


def test_descriptions_are_embedded_once_and_added_to_the_same_layer(shelf):
    put_file(shelf, A, shots=[shot(0, 0, 4)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, unit(2))])
    emb = FakeEmbedder()
    described = [{"id": 0, "start": 0.0, "end": 4.0, "description": "A dog runs on the beach."}]
    vectors.add_descriptions(shelf, A, emb, described)
    vectors.add_descriptions(shelf, A, emb, described)
    fi = load(shelf, A, "f1")
    assert [r["kind"] for r in fi.text_rows] == ["shot"]
    assert len(fi.image_rows) == 1
    assert sum(1 for c in emb.calls if c[0] == "texts") == 1


def test_descriptions_from_another_model_are_not_added_to_this_layer(shelf):
    put_file(shelf, A, shots=[shot(0, 0, 4)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, unit(2))], model=CLOUD)
    vectors.add_descriptions(shelf, A, FakeEmbedder(LOCAL), [{"id": 0, "start": 0.0, "end": 4.0, "description": "x"}])
    assert load(shelf, A, "f1").text_rows == []


# ---------------------------------------------------------------- ranking
def test_the_best_single_vector_ranks_a_shot_not_how_many_kinds_matched(shelf):
    # B matches through two kinds (picture and speech), A through one stronger picture. A plain rank fusion put B first.
    put_file(shelf, A, shots=[shot(0, 0, 5)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, at(2, 0.9))])
    put_file(shelf, B, shots=[shot(0, 0, 5)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, at(3, 0.85)),
                                                    ({"kind": "speech", "shot": 0, "start": 1.0, "end": 2.0, "text": "hi"}, at(4, 0.84))])
    hits = search.search([load(shelf, A, "fa"), load(shelf, B, "fb")], query_vector={LOCAL: unit(0)})["hits"]
    assert [h["file_id"] for h in hits] == ["fa", "fb"]
    assert hits[0]["peak"] == 2.0 and hits[0]["matched"] == "image"


def test_a_weak_best_match_is_no_match(shelf):
    weak = search.FLOOR[LOCAL] - 0.05
    put_file(shelf, A, shots=[shot(0, 0, 5)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, at(2, weak))])
    out = search.search([load(shelf, A, "fa")], query_vector={LOCAL: unit(0)})
    assert out["hits"] == [] and out["ranked"]


def test_far_below_the_top_hit_is_cut_even_above_the_floor(shelf):
    put_file(shelf, A, shots=[shot(0, 0, 5), shot(1, 5, 10)],
             rows=[({"kind": "image", "shot": 0, "t": 2.0}, at(2, 0.95)),
                   ({"kind": "image", "shot": 1, "t": 7.0}, at(3, 0.95 - search.RELATIVE_CUT - 0.02))])
    hits = search.search([load(shelf, A, "fa")], query_vector={LOCAL: unit(0)})["hits"]
    assert [h["shot_id"] for h in hits] == [0]


def test_exact_words_find_a_sentence_the_vectors_missed_and_snap_to_it(shelf):
    weak = search.FLOOR[LOCAL] - 0.1
    put_file(shelf, A, shots=[shot(0, 0, 10)], sentences=[{"start": 3.0, "end": 4.5, "text": "Order number 4471 has shipped"}],
             rows=[({"kind": "speech", "shot": 0, "start": 3.0, "end": 4.5, "text": "Order number 4471 has shipped"}, at(2, weak))])
    hits = search.search([load(shelf, A, "fa")], query_vector={LOCAL: unit(0)}, query_text="order 4471")["hits"]
    assert len(hits) == 1 and (hits[0]["start"], hits[0]["end"]) == (3.0, 4.5) and hits[0]["matched"] == "words"


def test_spoken_only_ignores_pictures(shelf):
    put_file(shelf, A, shots=[shot(0, 0, 5), shot(1, 5, 10)],
             rows=[({"kind": "image", "shot": 0, "t": 2.0}, at(2, 0.95)),
                   ({"kind": "speech", "shot": 1, "start": 6.0, "end": 7.0, "text": "x"}, at(3, 0.9))])
    hits = search.search([load(shelf, A, "fa")], query_vector={LOCAL: unit(0)}, look_for="spoken")["hits"]
    assert [h["shot_id"] for h in hits] == [1]


def test_each_file_is_searched_with_the_query_vector_of_its_own_model(shelf):
    put_file(shelf, A, shots=[shot(0, 0, 5)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, unit(0))], model=LOCAL)
    put_file(shelf, B, shots=[shot(0, 0, 5)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, unit(5))], model=CLOUD)
    files = [load(shelf, A, "fa"), load(shelf, B, "fb")]
    hits = search.search(files, query_vector={LOCAL: unit(7), CLOUD: unit(5)})["hits"]
    assert [h["file_id"] for h in hits] == ["fb"]


# ---------------------------------------------------------------- the worker
STUB_WORKER = textwrap.dedent("""
    import base64, json, sys
    import numpy as np
    print(json.dumps({"ready": True, "device": "cpu", "dtype": "q4"}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        if req["op"] == "shutdown":
            break
        n = len(req.get("texts") or req.get("paths") or [])
        if req["op"] == "embed_texts" and any("boom" in t for t in req["texts"]):
            print(json.dumps({"id": req["id"], "error": "model failed"}), flush=True)
            continue
        v = np.zeros(4, "<f4"); v[0 if "query:" in str(req.get("texts")) else 1] = 1
        print(json.dumps({"id": req["id"], "vectors": [base64.b64encode(v.tobytes()).decode()] * n}), flush=True)
""")


def test_the_worker_speaks_json_lines_and_adds_the_search_prefixes(tmp_path):
    script = tmp_path / "stub_worker.py"
    script.write_text(STUB_WORKER)
    emb = E.LocalEmbedder(command=[sys.executable, str(script)])
    try:
        doc, = emb.texts(["a sentence"])
        q, = emb.texts(["a query"], query=True)
        assert doc.tolist() == [0, 1, 0, 0] and q.tolist() == [1, 0, 0, 0]
        assert emb.device == "cpu" and emb.sent[-2]["texts"] == ["title: none | text: a sentence"]
        assert emb.sent[-1]["texts"] == ["task: search result | query: a query"]
        with pytest.raises(E.EmbedError):
            emb.texts(["boom"])
    finally:
        emb.close()


# ---------------------------------------------------------------- the model setting
def test_the_cloud_model_is_used_only_when_picked_and_signed_in(monkeypatch):
    monkeypatch.setattr(E, "_setting", lambda: "cloud")
    assert E.chosen_model(signed_in=True) == CLOUD
    assert E.chosen_model(signed_in=False) == LOCAL
    monkeypatch.setattr(E, "_setting", lambda: "")
    assert E.chosen_model(signed_in=True) == LOCAL


def test_the_cloud_model_sends_the_query_task_and_audio_as_wav(monkeypatch):
    client = MagicMock()
    v = base64.b64encode(np.ones(4, "<f2").tobytes()).decode()
    client.v2_embed.return_value = {"vectors": [v]}
    emb = E.CloudEmbedder(client=client)
    emb.texts(["a query"], query=True)
    items, kw = client.v2_embed.call_args[0][0], client.v2_embed.call_args[1]
    assert items == [{"kind": "text", "text": "a query"}] and kw["task_type"] == "RETRIEVAL_QUERY"
    emb.audio([(0.0, 10.0, np.zeros(16000 * 10, np.float32))])
    item = client.v2_embed.call_args[0][0][0]
    assert item["kind"] == "audio" and item["mime"] == "audio/wav"
    assert len(base64.b64decode(item["data"])) == 44 + 16000 * 10 * 2       # the backend bills the seconds in the WAV


# ---------------------------------------------------------------- the indexing job
class Job:
    def __init__(self, monkeypatch, tmp_path, *, signed_in=False, model_setting="", describe=False):
        self.shelf = Shelf(str(tmp_path / "shelf"))
        monkeypatch.setattr("classes.media_index.store.default_shelf", lambda: self.shelf)
        monkeypatch.setattr("classes.media_index.default_shelf", lambda: self.shelf)
        monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: signed_in))
        monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (True, 1000, None))
        monkeypatch.setattr(E, "_setting", lambda: model_setting)
        monkeypatch.setattr(E, "describe_on_import", lambda: describe)
        self.models = []

        def fake_embedder(model):
            self.models.append(model)
            return FakeEmbedder(model)
        monkeypatch.setattr(E, "get_embedder", fake_embedder)
        tmp_path.mkdir(parents=True, exist_ok=True)
        media = tmp_path / "clip.mp4"
        media.write_bytes(os.urandom(4096))
        self.sha = fingerprint(str(media))["sha256"]
        self.media = str(media)

        def facts(path, fingerprint=None, **kw):
            put_file(self.shelf, self.sha, shots=[shot(0, 0, 4)], rows=None)
            return {"ok": True}
        monkeypatch.setattr("classes.media_index.facts.compute_facts", facts)
        monkeypatch.setattr(vectors, "extract_keyframes", lambda path, plan, size, tmp: [f"{i}.jpg" for i in range(len(plan))])
        monkeypatch.setattr(vectors, "audio_windows", lambda path: [])
        monkeypatch.setattr("classes.media_index.probe.probe_media", lambda p: {"has_video": True, "duration": 4.0,
                                                                                  "video": {"width": 1280, "height": 720}})
        self.watch_calls = []
        monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: self.watch_calls.append(a) or {"layers": {}})
        self.client = MagicMock()
        self.client._empty_ai_metadata.side_effect = ZenviBackendClient._empty_ai_metadata

    def run(self):
        done = []
        IndexingJob({"id": "f1", "path": self.media, "media_type": "video", "duration": 4.0, "fingerprint": fingerprint(self.media)},
                    "p1", client_factory=lambda: self.client, emit_completed=lambda fd, md, err: done.append(md),
                    emit_progress=lambda *a: None).run()
        assert len(done) == 1
        return done[0]


def test_indexing_needs_no_sign_in_and_no_cloud_by_default(monkeypatch, tmp_path):
    job = Job(monkeypatch, tmp_path, signed_in=False)
    meta = job.run()
    assert meta["index"]["status"] == "ready"
    assert vectors.is_current(job.shelf, job.sha, LOCAL)
    assert job.watch_calls == [] and not job.client.v2_embed.called


def test_the_cloud_model_signed_out_falls_back_to_this_computer(monkeypatch, tmp_path):
    job = Job(monkeypatch, tmp_path, signed_in=False, model_setting="cloud")
    assert job.run()["index"]["status"] == "ready"
    assert job.models == [LOCAL]


def test_the_cloud_model_signed_in_makes_the_vectors_in_the_cloud(monkeypatch, tmp_path):
    job = Job(monkeypatch, tmp_path, signed_in=True, model_setting="cloud")
    job.run()
    assert job.models == [CLOUD] and vectors.is_current(job.shelf, job.sha, CLOUD)


def test_a_file_already_embedded_with_the_chosen_model_is_not_embedded_again(monkeypatch, tmp_path):
    job = Job(monkeypatch, tmp_path)
    job.run()
    job.models.clear()
    job.run()
    assert job.models == []


def test_descriptions_are_made_on_import_only_when_that_preference_is_on(monkeypatch, tmp_path):
    off = Job(monkeypatch, tmp_path / "off", signed_in=True)
    off.run()
    assert off.watch_calls == []
    on = Job(monkeypatch, tmp_path / "on", signed_in=True, describe=True)
    on.run()
    assert len(on.watch_calls) == 1


# ---------------------------------------------------------------- descriptions on demand
def test_describe_shots_pays_only_for_shots_not_yet_described(shelf, monkeypatch):
    put_file(shelf, A, shots=[shot(0, 0, 4), shot(1, 4, 8)], rows=[({"kind": "image", "shot": 0, "t": 2.0}, unit(2))],
             watch=[{"id": 0, "start": 0.0, "end": 4.0, "description": "Already described."}])
    asked = []

    def window(client, path, probe, file_id, shots, transcript, **kw):
        asked.append([s["id"] for s in shots])
        return {"shots": [{"id": s["id"], "start": s["start"], "end": s["end"], "description": f"Shot {s['id']}."} for s in shots],
                "usage": {"model": "flash-lite"}}
    monkeypatch.setattr(cloud, "_watch_window", window)
    emb = FakeEmbedder()
    out = cloud.describe_shots(MagicMock(), "clip.mp4", {"duration": 8.0}, A, shelf, [0, 1], emb, uploader=lambda *a, **k: None)
    assert asked == [[1]] and out["described"] == [1]
    watch = json.loads(Path(shelf.entry_dir(A), "watch.json").read_text())
    assert {s["id"]: s["description"] for s in watch["shots"]} == {0: "Already described.", 1: "Shot 1."}
    again = cloud.describe_shots(MagicMock(), "clip.mp4", {"duration": 8.0}, A, shelf, [0, 1], emb, uploader=lambda *a, **k: None)
    assert asked == [[1]] and again["described"] == []
    assert sorted(r["shot"] for r in load(shelf, A, "fa").text_rows) == [0, 1]
