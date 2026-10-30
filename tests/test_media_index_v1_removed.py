"""The v1 indexing pipeline is gone: media index v2 is the only way a file is indexed.

Drives IndexingJob with the fakes in test_indexing_job_reuse.py. Nothing here reaches the network or ffmpeg.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.api_client import ZenviBackendClient  # noqa: E402
from classes.media_index import flags  # noqa: E402
from classes.media_index.job import IndexingJob  # noqa: E402
from test_indexing_job_reuse import Harness, _stub_vectors  # noqa: E402

V1_CLIENT_METHODS = ("search", "watch_window", "start_direct_indexing_job", "summarize_indexed_video",
                     "restore_index", "reindex_video", "is_indexing_configured")


def _no_old_route(h):
    h.client.start_direct_indexing_job.assert_not_called()
    h.client.restore_index.assert_not_called()
    h.client.is_indexing_configured.assert_not_called()


def test_a_file_with_no_fingerprint_fails_with_that_reason(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    monkeypatch.setattr(IndexingJob, "_fingerprint_sha", lambda self, path: "")
    meta, _ = h.run(with_fingerprint=False)
    assert meta["index"]["status"] == "failed" and "no fingerprint" in meta["error"]
    assert h.charges == []
    _no_old_route(h)


def test_a_file_already_on_the_shelf_is_restored_without_embedding_again(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    calls = _stub_vectors(monkeypatch, h)
    h.run(duration=6.0)
    meta, _ = h.run(file_id="f2", project="p2", duration=6.0)
    assert len(calls) == 1 and h.charges == [] and meta["index"]["index_id"] == "zenvi-p2"
    _no_old_route(h)


def test_footage_that_shares_a_fingerprint_with_other_footage_fails_with_that_reason(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    calls = _stub_vectors(monkeypatch, h)
    monkeypatch.setattr("classes.media_index.facts.compute_facts", lambda *a, **k: {"ok": False, "collision": True, "layers": {}})
    meta, _ = h.run(duration=6.0)
    assert calls == [] and meta["index"]["status"] == "failed" and "different file" in meta["error"]
    _no_old_route(h)


def test_an_editor_duration_that_is_unknown_or_off_by_a_second_never_fails_the_file(monkeypatch, tmp_path):
    for editor_duration in (0.0, 7.5):          # the shelf holds ffprobe's 6.0 s for this very file
        (tmp_path / str(editor_duration)).mkdir()
        h = Harness(monkeypatch, tmp_path / str(editor_duration))
        _stub_vectors(monkeypatch, h)
        h.shelf.set_source(h.sha, duration=6.0, media_type="video")
        meta, _ = h.run(duration=editor_duration)
        assert meta["index"]["status"] != "failed", (editor_duration, meta.get("error"))


def test_local_facts_never_overwrite_the_record_of_different_footage(monkeypatch, tmp_path):
    from classes.media_index import facts
    from classes.media_index.store import Shelf
    shelf = Shelf(str(tmp_path / "shelf"))
    shelf.set_source("a" * 64, duration=900.0, media_type="video")
    monkeypatch.setattr(facts, "probe_media", lambda path: {"ok": True, "duration": 6.0, "has_video": True, "has_audio": False, "video": {}})
    out = facts.compute_facts("x.mp4", fingerprint="a" * 64, shelf=shelf, media_type="video")
    assert out["ok"] is False and out.get("collision") is True
    assert (shelf.read_json("a" * 64, "manifest.json") or {})["source"]["duration"] == 900.0


def test_flags_has_no_v2_switch():
    assert not hasattr(flags, "v2_enabled")


def test_the_api_client_loses_every_v1_indexing_method_and_keeps_v2():
    assert [m for m in V1_CLIENT_METHODS if hasattr(ZenviBackendClient, m)] == []
    for name in ("v2_upload_session", "v2_understand", "v2_describe_audio", "v2_listen", "v2_job", "v2_embed"):
        assert callable(getattr(ZenviBackendClient, name))
