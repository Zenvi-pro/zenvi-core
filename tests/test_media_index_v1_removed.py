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
from test_indexing_job_reuse import Harness, _v2  # noqa: E402

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


def test_a_backend_without_media_index_v2_fails_the_file_with_that_reason(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, result={"unsupported": True, "error": "no media index v2", "layers": {}})
    meta, _ = h.run(duration=6.0)
    assert len(calls) == 1
    assert meta["index"]["status"] == "failed" and "has no media index v2" in meta["error"]
    assert h.charges == []
    _no_old_route(h)


def test_a_shelf_restore_is_free_and_never_calls_restore_index(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, populate=True)
    meta, _ = h.run(file_id="f2", project="p2", duration=6.0)
    assert calls == [] and h.charges == []
    assert meta["index"]["index_id"] == "zenvi-p2" and meta["chapters"]
    _no_old_route(h)


def test_footage_that_shares_a_fingerprint_with_other_footage_fails_with_that_reason(monkeypatch, tmp_path):
    h, calls = _v2(monkeypatch, tmp_path, populate=True)
    h.shelf.set_source(h.sha, duration=900.0, media_type="video")
    meta, _ = h.run(duration=6.0)
    assert calls == [] and meta["index"]["status"] == "failed" and "different file" in meta["error"]
    _no_old_route(h)


def test_flags_has_no_v2_switch():
    assert not hasattr(flags, "v2_enabled")


def test_the_api_client_loses_every_v1_indexing_method_and_keeps_v2():
    assert [m for m in V1_CLIENT_METHODS if hasattr(ZenviBackendClient, m)] == []
    for name in ("v2_upload_session", "v2_understand", "v2_describe_audio", "v2_listen", "v2_job", "v2_embed"):
        assert callable(getattr(ZenviBackendClient, name))
