"""reindex_project_file_tool on a file whose index is incomplete goes through the normal indexing job, not only the older upload.

Found live: it reported "index ready" for a file whose shelf had no layers, and charged separately for the older upload.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import tool_handlers as th  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402


def _setup(monkeypatch, tmp_path, *, ready=(), busy=False):
    import classes.query as query
    shelf = Shelf(str(tmp_path))
    key = "ef" * 32
    for layer in ready:
        shelf.set_layer(key, layer, version=9, status="ready")
    monkeypatch.setattr("classes.editor_tools.media_index_tools.default_shelf", lambda: shelf)
    files_model = MagicMock()
    files_model.is_file_indexing.return_value = busy
    files_model.is_file_queued.return_value = False
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=SimpleNamespace(files_model=files_model)))
    fake = SimpleNamespace(data={"fingerprint": {"sha256": key}, "media_type": "video"})
    monkeypatch.setattr(query.File, "get", classmethod(lambda cls, **kw: fake))
    return files_model


def test_a_file_missing_layers_is_queued_on_the_normal_indexing_job(monkeypatch, tmp_path):
    fm = _setup(monkeypatch, tmp_path, ready=("structure", "look"))
    out = th._queue_missing_index_layers("f1")
    fm._index_file_async.assert_called_once_with("f1")
    assert "queued" in out and "audio" in out and "watch" in out and "wait_until_project_indexed_tool" in out


def test_a_file_already_being_indexed_is_not_queued_twice(monkeypatch, tmp_path):
    fm = _setup(monkeypatch, tmp_path, busy=True)
    assert "already being indexed" in th._queue_missing_index_layers("f1")
    fm._index_file_async.assert_not_called()


def test_a_complete_file_is_not_queued_again(monkeypatch, tmp_path):
    fm = _setup(monkeypatch, tmp_path, ready=("structure", "look", "audio", "speech", "watch", "vectors"))
    assert th._queue_missing_index_layers("f1") == ""
    fm._index_file_async.assert_not_called()


def test_reindex_of_a_complete_file_says_so_and_never_sends_the_older_upload(monkeypatch, tmp_path):
    from classes import api_client, credits_client
    fm = _setup(monkeypatch, tmp_path, ready=("structure", "look", "audio", "speech", "watch", "vectors"))
    cloud = MagicMock()
    monkeypatch.setattr(api_client, "get_backend_client", lambda: cloud)
    monkeypatch.setattr(credits_client, "check_operation", lambda *a, **k: (True, 0, None))
    out = th.reindex_project_file(file_id="f1")
    assert "nothing to re-index" in out
    fm._index_file_async.assert_not_called()
    cloud.reindex_video.assert_not_called()


def test_reindex_of_a_file_missing_layers_queues_the_index_job_only(monkeypatch, tmp_path):
    from classes import api_client
    fm = _setup(monkeypatch, tmp_path, ready=("structure",))
    cloud = MagicMock()
    monkeypatch.setattr(api_client, "get_backend_client", lambda: cloud)
    out = th.reindex_project_file(file_id="f1")
    assert "queued" in out
    fm._index_file_async.assert_called_once_with("f1")
    cloud.reindex_video.assert_not_called()


def test_reindex_takes_no_force_flag_because_it_never_changed_anything():
    from classes.agent_tools.schema import TOOL_SCHEMAS

    assert "force" not in TOOL_SCHEMAS["reindex_project_file_tool"]["properties"]
