"""Re-indexing a file after a failed index must re-run and refresh what is shown (#143).

Headless: BackendIndexingWorker and FilesModel subclass Qt types, so their
methods are compiled straight from source (as test_indexing_queue.py does) and
run against fakes. The Scene Descriptions panel builds on the conftest Qt stub.
"""

import ast
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from classes import api_client  # noqa: E402
from classes import credits_client as cc  # noqa: E402
from classes import info  # noqa: E402
from classes import query  # noqa: E402
from classes import tool_handlers as th  # noqa: E402
from classes.ai_metadata_utils import (  # noqa: E402
    get_effective_ai_metadata,
    materialize_clip_ai_metadata,
    merge_indexing_result,
)
from classes.api_client import ZenviBackendClient  # noqa: E402
from classes.indexing_status import FAILED, RUNNING, SUCCESS, derive_indexing_status  # noqa: E402
from classes.media_cache import save_ai_metadata  # noqa: E402
from classes.twelvelabs_match import index_is_complete  # noqa: E402

FILES_MODEL = os.path.join(SRC, "windows", "models", "files_model.py")

READY = {"status": "ready", "index_id": "zenvi-p1", "video_id": "f1", "provider": "gemini"}

# What a finished Gemini indexing job hands back for a 6 s clip.
FRESH = {
    "analyzed": True,
    "short_summary": "A dog runs on a beach.",
    "description": "A brown dog sprints along the shoreline.",
    "transcript": "good boy",
    "transcript_cues": [{"start": 1.0, "end": 2.0, "text": "good boy"}],
    "scene_descriptions": [{"description": "dog at the waterline", "source_time": 2.0, "time": 2.0}],
    "index": dict(READY),
    "twelvelabs": dict(READY),
}

# A run that saved its index handles and then failed: the shape that used to
# read as "already indexed" forever.
FAILED_WITH_HANDLES = {
    "analyzed": False,
    "error": "Description generation failed",
    "index": dict(READY),
    "twelvelabs": dict(READY),
}


def _compile_methods(class_name, names, **module_globals):
    """Compile real methods of a files_model.py class without importing Qt."""
    with open(FILES_MODEL, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = dict(module_globals)
    exec(compile(ast.Module(body=funcs, type_ignores=[]), FILES_MODEL, "exec"), ns)
    return {n: ns[n] for n in names}


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _FakeFile:
    def __init__(self, data):
        self.id = data.get("id")
        self.key = ["files", {"id": self.id}]
        self.data = data
        self.saved = 0

    def save(self):
        self.saved += 1

    def get_ai_metadata(self):
        return self.data.get("ai_metadata", {})


# ── the "already indexed" gate ─────────────────────────────────────────────


def test_ready_handles_left_by_a_failed_run_are_not_a_complete_index():
    assert index_is_complete(FAILED_WITH_HANDLES) is False


def test_a_clean_ready_index_is_complete_under_either_key():
    analysis = {k: v for k, v in FRESH.items() if k not in ("index", "twelvelabs")}
    assert index_is_complete(dict(analysis, index=dict(READY))) is True
    assert index_is_complete(dict(analysis, twelvelabs=dict(READY))) is True
    assert index_is_complete(FRESH) is True


def test_ready_handles_without_any_analysis_are_not_a_complete_index():
    assert index_is_complete({"index": dict(READY)}) is False
    assert index_is_complete({"analyzed": True, "index": dict(READY), "twelvelabs": dict(READY)}) is False


def test_interrupted_and_failed_blocks_are_not_complete():
    assert index_is_complete({"index": {"status": "indexing", "index_id": "i", "video_id": "v"}}) is False
    assert index_is_complete({"index": {"status": "failed", "error": "upload rejected"}}) is False
    assert index_is_complete(None) is False


# ── BackendIndexingWorker.run ──────────────────────────────────────────────


def _run_worker(monkeypatch, ai_metadata, *, force=False):
    """Run BackendIndexingWorker.run synchronously against a fake backend client."""
    monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (True, 1000, None))
    monkeypatch.setattr(cc, "charge_operation_on_success", lambda *a, **k: None)
    client = MagicMock()
    client._empty_ai_metadata.side_effect = ZenviBackendClient._empty_ai_metadata
    client.is_indexing_configured.return_value = True
    client.start_direct_indexing_job.return_value = {
        "success": True, "index_id": "zenvi-p1", "video_id": "f1", "ai_metadata": dict(FRESH),
    }
    run = _compile_methods(
        "BackendIndexingWorker", ["run"], get_backend_client=lambda: client, log=MagicMock(),
    )["run"]
    worker = types.SimpleNamespace(
        file_data={"id": "f1", "path": "/media/clip.mp4", "media_type": "video",
                   "duration": 6.0, "ai_metadata": ai_metadata},
        project_id="p1",
        force=force,
        completed=_Signal(),
        progress=_Signal(),
        intermediate_save=_Signal(),
        _MAX_INDEXING_SECONDS=30 * 60,
    )
    run(worker)
    return client, worker


def test_a_file_whose_index_failed_after_saving_handles_is_indexed_again(monkeypatch):
    client, worker = _run_worker(monkeypatch, dict(FAILED_WITH_HANDLES))

    client.start_direct_indexing_job.assert_called_once()
    [(_data, metadata, error)] = worker.completed.emitted
    assert error is None
    assert metadata["analyzed"] is True
    assert metadata["description"] == FRESH["description"]
    assert "error" not in metadata


def test_a_cleanly_indexed_file_is_still_not_indexed_again_on_import(monkeypatch):
    client, _ = _run_worker(monkeypatch, dict(FRESH))

    client.start_direct_indexing_job.assert_not_called()


def test_reindex_runs_again_on_a_clean_index_and_tells_the_backend(monkeypatch):
    client, worker = _run_worker(monkeypatch, dict(FRESH), force=True)

    client.start_direct_indexing_job.assert_called_once()
    assert client.start_direct_indexing_job.call_args.kwargs["force"] is True
    [(_data, metadata, _error)] = worker.completed.emitted
    # The old summarize-only branch answered with this dead end instead.
    assert "Summarize-only" not in str(metadata.get("error") or "")


# ── what a file holds after an attempt ─────────────────────────────────────


def test_a_usable_retry_replaces_the_failed_state_and_is_dated():
    merged = merge_indexing_result(dict(FAILED_WITH_HANDLES), dict(FRESH))

    assert "error" not in merged
    assert merged["description"] == FRESH["description"]
    assert merged["analysis_date"]
    assert derive_indexing_status(merged).state == SUCCESS


def test_a_failed_retry_keeps_good_content_but_reports_its_own_error():
    previous = dict(FRESH, error="first failure")
    merged = merge_indexing_result(
        previous, {"analyzed": False, "error": "second failure", "index": {"status": "failed"}},
    )

    assert merged["description"] == FRESH["description"]
    st = derive_indexing_status(merged)
    assert (st.state, st.tooltip) == (FAILED, "second failure")


def test_a_failure_reported_only_on_its_index_block_replaces_the_old_error():
    previous = dict(FRESH, error="first failure")
    merged = merge_indexing_result(previous, {"index": {"status": "failed", "error": "upload rejected"}})

    st = derive_indexing_status(merged)
    assert (st.state, st.tooltip) == (FAILED, "upload rejected")


def test_failure_then_failure_shows_the_new_error():
    first = merge_indexing_result(None, {"analyzed": False, "error": "first failure"})
    second = merge_indexing_result(first, {"analyzed": False, "error": "second failure"})

    assert derive_indexing_status(second).tooltip == "second failure"


def test_the_in_progress_stub_does_not_carry_the_old_error_forward():
    previous = dict(FRESH, error="first failure")
    merged = merge_indexing_result(previous, {"analyzed": False, "index": {"status": "indexing"}})

    assert "error" not in merged
    assert derive_indexing_status(merged, is_active=True).state == RUNNING
    # The app dies mid-run: the badge reads interrupted, not the stale failure.
    assert "interrupted" in derive_indexing_status(merged).tooltip.lower()


# ── FilesModel: storing an outcome ─────────────────────────────────────────


def _files_model(monkeypatch, files):
    """Real FilesModel methods over fake project files; returns (model, window, cache_writes)."""
    window = types.SimpleNamespace(FileUpdated=_Signal(), schedule_flush_project_to_disk=lambda: None)
    app = types.SimpleNamespace(window=window, updates=MagicMock())
    window.updates = app.updates
    file_lookup = types.SimpleNamespace(get=lambda **kw: files.get(kw.get("id")))
    names = [
        "_apply_ai_metadata", "apply_indexing_result", "can_reindex_file", "reindex_file",
        "_enqueue_index", "_drain_indexing_queue", "is_file_indexing", "is_file_queued",
    ]
    methods = _compile_methods("FilesModel", names, File=file_lookup, get_app=lambda: app, log=MagicMock())
    model = type("FilesModelMethods", (), methods)()
    model._status_cache = {}
    model._indexing_queue = []
    model._active_indexers = []
    model._MAX_INDEXING_WORKERS = 2
    model.started = []

    def _start(fid, force=False):
        model.started.append((fid, force))
        model._active_indexers.append(types.SimpleNamespace(file_data={"id": fid}))

    model._start_indexing_worker = _start
    cache_writes = []
    monkeypatch.setattr(
        "classes.media_cache.save_ai_metadata", lambda fp, meta: cache_writes.append(meta) or True,
    )
    return model, window, cache_writes


def _video(ai_metadata, file_id="f1"):
    return _FakeFile({"id": file_id, "media_type": "video", "fingerprint": {"sha256": "ab" * 32},
                      "ai_metadata": ai_metadata})


def test_a_successful_retry_clears_the_error_caches_and_repaints(monkeypatch):
    f = _video(dict(FAILED_WITH_HANDLES))
    model, window, cache_writes = _files_model(monkeypatch, {"f1": f})
    model._status_cache["f1"] = "stale FAILED badge"

    model.apply_indexing_result("f1", dict(FRESH))

    assert "error" not in f.data["ai_metadata"]
    assert derive_indexing_status(f.data["ai_metadata"]).state == SUCCESS
    assert [w["description"] for w in cache_writes] == [FRESH["description"]]
    assert f.saved == 1
    assert window.FileUpdated.emitted == [("f1",)]
    assert "f1" not in model._status_cache


def test_a_failed_retry_after_good_analysis_keeps_it_and_records_the_new_error(monkeypatch):
    f = _video(dict(FRESH, error="first failure"))
    model, _, cache_writes = _files_model(monkeypatch, {"f1": f})

    model.apply_indexing_result(
        "f1", {"analyzed": False, "error": "second failure", "index": {"status": "failed"}},
    )

    meta = f.data["ai_metadata"]
    assert meta["description"] == FRESH["description"]
    assert derive_indexing_status(meta).tooltip == "second failure"
    # Nothing new to cache: a failure must not write files from the GUI thread.
    assert cache_writes == []


def test_a_failed_result_is_stored_without_an_undo_step(monkeypatch):
    f = _video(dict(FRESH))
    model, window, _ = _files_model(monkeypatch, {"f1": f})

    model.apply_indexing_result("f1", {"analyzed": False, "error": "boom", "index": {"status": "failed"}})

    assert f.saved == 0  # File.save() would add an undo-history action
    window.updates.update_untracked.assert_called_once_with(f.key, f.data)
    assert window.FileUpdated.emitted == [("f1",)]


def test_a_failure_with_nothing_good_to_keep_is_not_cached(monkeypatch):
    f = _video({})
    model, _, cache_writes = _files_model(monkeypatch, {"f1": f})

    model.apply_indexing_result("f1", {"analyzed": False, "error": "boom"})

    assert cache_writes == []
    assert derive_indexing_status(f.data["ai_metadata"]).tooltip == "boom"


def test_reindex_queues_one_forced_run(monkeypatch):
    model, _, _ = _files_model(monkeypatch, {"f1": _video(dict(FRESH))})

    model.reindex_file("f1")
    model.reindex_file("f1")  # already running: not queued twice

    assert model.started == [("f1", True)]


def test_reindex_is_not_offered_while_indexing_or_for_media_that_is_never_indexed(monkeypatch):
    title = _FakeFile({"id": "t1", "media_type": "title", "ai_metadata": {}})
    model, _, _ = _files_model(monkeypatch, {"f1": _video(dict(FRESH)), "t1": title})
    model._active_indexers = [types.SimpleNamespace(file_data={"id": "f1"})]

    assert model.can_reindex_file("f1") is False
    assert model.can_reindex_file("t1") is False
    assert model.can_reindex_file("missing") is False
    model.reindex_file("f1")
    assert model.started == []


# ── timeline clips and the fingerprint cache ───────────────────────────────


def _dated(meta, date, description):
    scenes = [{"description": description + " scene", "source_time": 2.0, "time": 2.0}]
    return dict(meta, analysis_date=date, description=description, scene_descriptions=scenes)


def test_a_clip_snapshot_from_before_a_reindex_is_recomputed():
    old_root = _dated(FRESH, "2026-09-01T10:00:00+00:00", "old run")
    new_root = _dated(FRESH, "2026-09-30T10:00:00+00:00", "new run")
    # Not 0-based: clip_metadata_is_valid reads a 0.0 start as -1 and always recomputes.
    clip = {"start": 1.0, "end": 6.0, "ai_metadata": materialize_clip_ai_metadata(old_root, 1.0, 6.0)}

    effective = get_effective_ai_metadata(
        {"ai_metadata": new_root}, clip, clip_ai_metadata=clip["ai_metadata"],
    )

    assert effective["description"] == "new run"
    assert [s["description"] for s in effective["scene_descriptions"]] == ["new run scene"]


def test_a_snapshot_of_the_current_analysis_is_still_used():
    root = _dated(FRESH, "2026-09-30T10:00:00+00:00", "current run")
    snapshot = dict(materialize_clip_ai_metadata(root, 1.0, 6.0), description="as saved on the clip")
    clip = {"start": 1.0, "end": 6.0, "ai_metadata": snapshot}

    effective = get_effective_ai_metadata({"ai_metadata": root}, clip, clip_ai_metadata=snapshot)

    assert effective["description"] == "as saved on the clip"


def test_an_analyzed_file_is_not_mixed_with_an_older_cached_run(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    fp = {"sha256": "cd" * 32}
    save_ai_metadata(fp, dict(FRESH, transcript="words from the first run", error="first failure"))
    retried = dict(FRESH, transcript="", transcript_cues=[])  # the new run heard no speech

    effective = get_effective_ai_metadata(
        {"fingerprint": fp, "ai_metadata": retried}, {"start": 0.0, "end": 6.0},
    )

    assert effective["transcript"] == ""
    assert effective["transcript_cues"] == []


def test_a_file_that_only_holds_index_handles_is_still_filled_from_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    fp = {"sha256": "ef" * 32}
    save_ai_metadata(fp, dict(FRESH))

    effective = get_effective_ai_metadata(
        {"fingerprint": fp, "ai_metadata": {"index": dict(READY)}}, {"start": 0.0, "end": 6.0},
    )

    assert effective["description"] == FRESH["description"]


# ── reindex_project_file (agent tool / MCP) ────────────────────────────────


def _reindex_tool(monkeypatch, *, ai_metadata=None, result=None, force="false", duration=6.0,
                  configured=True, blocked=None, file_exists=True, file_id="f1"):
    """Call the tool against a fake project file and backend; return (out, client, stored, charges)."""
    stored = []
    files_model = types.SimpleNamespace(apply_indexing_result=lambda fid, meta: stored.append((fid, meta)))
    fake_file = _FakeFile({"id": "f1", "path": "/media/clip.mp4", "duration": duration,
                           "ai_metadata": ai_metadata or {}})
    monkeypatch.setattr(query.File, "get", lambda **kw: fake_file if file_exists else None)
    app = types.SimpleNamespace(project={"id": "p1"}, window=types.SimpleNamespace(files_model=files_model))
    monkeypatch.setattr(th, "_get_app", lambda: app)
    # No Qt event loop runs here, stubbed or real: run the main-thread hops inline.
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, timeout=None: fn(*a))
    charges = []
    monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (blocked is None, 1000, blocked))
    monkeypatch.setattr(cc, "charge_operation_on_success", lambda *a, **k: charges.append((a, k)))
    client = MagicMock()
    client.is_indexing_configured.return_value = configured
    if isinstance(result, Exception):
        client.reindex_video.side_effect = result
    else:
        client.reindex_video.return_value = result
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)
    return th.reindex_project_file(file_id=file_id, force=force), client, stored, charges


SUCCESS_RESULT = {"success": True, "file_id": "f1", "index_id": "zenvi-p1", "video_id": "f1",
                  "ai_metadata": dict(FRESH)}


def test_the_tool_reindexes_a_failed_file_without_needing_force(monkeypatch):
    out, client, _, charges = _reindex_tool(
        monkeypatch, ai_metadata=dict(FAILED_WITH_HANDLES), result=dict(SUCCESS_RESULT),
    )

    client.reindex_video.assert_called_once()
    assert out.startswith("Re-indexing complete"), out
    assert len(charges) == 1


def test_a_successful_reindex_stores_the_whole_fresh_analysis(monkeypatch):
    """The job's description, scenes and transcript cues must not be thrown away."""
    out, client, stored, _ = _reindex_tool(
        monkeypatch, ai_metadata=dict(FAILED_WITH_HANDLES), result=dict(SUCCESS_RESULT),
    )

    [(file_id, meta)] = stored
    assert file_id == "f1"
    for key in ("description", "short_summary", "transcript", "transcript_cues", "scene_descriptions"):
        assert meta[key] == FRESH[key], key
    assert meta["analyzed"] is True
    assert meta["index"]["status"] == "ready"
    assert meta["twelvelabs"] == meta["index"]
    # Gemini has no summarize-only step; it only ever answered with an error.
    client.summarize_indexed_video.assert_not_called()
    assert "Summar" not in out and "Warning" not in out


def test_a_stored_reindex_result_clears_the_old_error_end_to_end(monkeypatch):
    """Through the real FilesModel merge: what the badge and the panel then read."""
    f = _video(dict(FAILED_WITH_HANDLES))
    model, window, _ = _files_model(monkeypatch, {"f1": f})
    _, _, stored, _ = _reindex_tool(monkeypatch, ai_metadata=dict(FAILED_WITH_HANDLES),
                                    result=dict(SUCCESS_RESULT))
    for file_id, meta in stored:
        model.apply_indexing_result(file_id, meta)

    meta = f.data["ai_metadata"]
    assert derive_indexing_status(meta).state == SUCCESS
    assert "error" not in meta
    assert meta["description"] == FRESH["description"]
    assert meta["transcript_cues"] == FRESH["transcript_cues"]
    assert window.FileUpdated.emitted == [("f1",)]


def test_a_failed_reindex_stores_its_own_error(monkeypatch):
    out, _, stored, charges = _reindex_tool(
        monkeypatch, ai_metadata={"analyzed": False, "error": "first failure"},
        result={"success": False, "error": "second failure"},
    )

    assert out == "Error: Re-indexing failed: second failure"
    [(_fid, meta)] = stored
    assert derive_indexing_status(merge_indexing_result({"error": "first failure"}, meta)).tooltip == (
        "second failure"
    )
    assert charges == []


def test_force_reindexes_a_cleanly_indexed_file(monkeypatch):
    _, client, _, _ = _reindex_tool(
        monkeypatch, ai_metadata=dict(FRESH), result=dict(SUCCESS_RESULT), force="true",
    )

    client.reindex_video.assert_called_once()
    assert client.reindex_video.call_args.kwargs["force"] is True


def test_a_cleanly_indexed_file_is_not_reindexed_without_force(monkeypatch):
    out, client, stored, charges = _reindex_tool(monkeypatch, ai_metadata=dict(FRESH))

    assert "already indexed" in out
    client.reindex_video.assert_not_called()
    assert stored == [] and charges == []


@pytest.mark.parametrize("case", [
    {"file_id": ""},
    {"file_exists": False},
    {"duration": 31 * 60},
    {"configured": False},
    {"blocked": "Not enough credits for video re-indexing"},
    {"result": {"success": False, "error": "upload rejected"}},
    {"result": {"success": False}},
    {"result": RuntimeError("socket closed")},
], ids=["no-file-id", "not-found", "too-long", "not-configured", "credits", "backend-failed",
        "no-reason", "exception"])
def test_every_failure_of_the_tool_reads_as_an_error(monkeypatch, case):
    """Agents, the chat UI and MCP treat anything without an "Error:" prefix as success."""
    out, _, _, charges = _reindex_tool(monkeypatch, ai_metadata=dict(FAILED_WITH_HANDLES), **case)

    assert out.startswith("Error:"), out
    assert charges == []


# ── the Scene Descriptions panel ───────────────────────────────────────────

# Builds the dock on the conftest Qt stub; under ZENVI_REAL_QT=1 there is no
# QApplication to parent real widgets to.
needs_qt_stub = pytest.mark.skipif(
    os.environ.get("ZENVI_REAL_QT") == "1", reason="builds the panel on the conftest Qt stub",
)


def _stubbed(panel_class):
    """The panel over the conftest Qt stub.

    Stub Qt classes are MagicMocks, and a mock builds its child attributes
    (``self.setObjectName``...) by calling its own class, i.e. the panel's
    ``__init__``. Answer those with plain mocks instead.
    """
    return type("StubbedAIMediaPanel", (panel_class,), {"_get_child_mock": lambda self, **kw: MagicMock(**kw)})


def _panel(monkeypatch, file_data, clip_data=None):
    """An AIMediaPanel over one project file (and optionally one selected timeline clip)."""
    from windows import ai_media_panel

    f = _FakeFile(file_data)
    clip = types.SimpleNamespace(id="c1", data=clip_data) if clip_data else None
    monkeypatch.setattr(query.File, "get", lambda **kw: f if kw.get("id") == f.id else None)
    monkeypatch.setattr(query.Clip, "get", lambda **kw: clip if kw.get("id") == "c1" else None)
    files_model = types.SimpleNamespace(
        selection_model=MagicMock(),
        indexingProgress=MagicMock(),
        current_file=lambda: f,
        get_indexing_progress=lambda fid: None,
        is_file_indexing=lambda fid: False,
        is_file_queued=lambda fid: False,
        can_reindex_file=lambda fid: True,
        reindex_file=MagicMock(),
    )
    files_view = MagicMock()
    files_view.hasFocus.return_value = clip is None
    window = types.SimpleNamespace(
        files_model=files_model,
        FileUpdated=MagicMock(),
        SelectionChanged=MagicMock(),
        filesView=files_view,
        selected_clips=["c1"] if clip else [],
    )
    monkeypatch.setattr(ai_media_panel, "get_app", lambda: types.SimpleNamespace(window=window))
    # The stub's QTimer is a bare MagicMock, which would take the panel as its spec.
    monkeypatch.setattr(ai_media_panel, "QTimer", lambda *a, **k: MagicMock())
    return _stubbed(ai_media_panel.AIMediaPanel)(), f, files_model


def _body(panel):
    return panel.description_view.setPlainText.call_args.args[0]


def _status_line(panel):
    return panel.indexing_status_label.setText.call_args.args[0]


@needs_qt_stub
def test_the_panel_shows_the_new_description_once_a_retry_succeeds(monkeypatch):
    panel, f, _ = _panel(monkeypatch, {"id": "f1", "name": "clip.mp4", "media_type": "video",
                                       "ai_metadata": {"analyzed": False, "error": "first failure"}})
    assert _body(panel) == "first failure"
    assert _status_line(panel) == "first failure"

    f.data["ai_metadata"] = merge_indexing_result(f.data["ai_metadata"], dict(FRESH))
    panel.indexing_status_label.reset_mock()
    panel._on_file_updated("f1")  # FileUpdated after the retry: no reselection

    assert FRESH["description"] in _body(panel)
    panel.indexing_status_label.hide.assert_called_once()
    panel.indexing_status_label.show.assert_not_called()


@needs_qt_stub
def test_the_panel_shows_the_second_error_after_failure_then_failure(monkeypatch):
    panel, f, _ = _panel(monkeypatch, {"id": "f1", "name": "clip.mp4", "media_type": "video",
                                       "ai_metadata": {"analyzed": False, "error": "first failure"}})

    f.data["ai_metadata"] = merge_indexing_result(
        f.data["ai_metadata"], {"analyzed": False, "error": "second failure"},
    )
    panel._on_file_updated("f1")

    assert _body(panel) == "second failure"
    assert _status_line(panel) == "second failure"


@needs_qt_stub
def test_a_timeline_clip_shows_its_reindexed_source_not_its_saved_snapshot(monkeypatch):
    old_root = _dated(FRESH, "2026-09-01T10:00:00+00:00", "old run")
    new_root = _dated(FRESH, "2026-09-30T10:00:00+00:00", "new run")
    snapshot = materialize_clip_ai_metadata(old_root, 0.0, 6.0)
    panel, _, _ = _panel(
        monkeypatch,
        {"id": "f1", "name": "clip.mp4", "media_type": "video", "duration": 6.0, "ai_metadata": new_root},
        {"id": "c1", "file_id": "f1", "title": "clip", "start": 0.0, "end": 6.0, "ai_metadata": snapshot},
    )

    assert "new run" in _body(panel)
    assert "old run" not in _body(panel)


@needs_qt_stub
def test_a_timeline_clip_keeps_its_snapshot_when_its_source_has_no_analysis(monkeypatch):
    snapshot = materialize_clip_ai_metadata(_dated(FRESH, "2026-09-01T10:00:00+00:00", "old run"), 0.0, 6.0)
    panel, _, _ = _panel(
        monkeypatch,
        {"id": "f1", "name": "clip.mp4", "media_type": "video", "duration": 6.0, "ai_metadata": {}},
        {"id": "c1", "file_id": "f1", "title": "clip", "start": 0.0, "end": 6.0, "ai_metadata": snapshot},
    )

    assert "old run" in _body(panel)


@needs_qt_stub
def test_a_timeline_clip_reports_a_failed_retry_of_its_source(monkeypatch):
    snapshot = materialize_clip_ai_metadata(dict(FRESH), 0.0, 6.0)
    panel, _, _ = _panel(
        monkeypatch,
        {"id": "f1", "name": "clip.mp4", "media_type": "video", "duration": 6.0,
         "ai_metadata": dict(FRESH, error="second failure")},
        {"id": "c1", "file_id": "f1", "title": "clip", "start": 0.0, "end": 6.0, "ai_metadata": snapshot},
    )

    assert _status_line(panel) == "second failure"


@needs_qt_stub
def test_the_panel_reindex_button_reindexes_the_clips_source_file(monkeypatch):
    panel, _, files_model = _panel(
        monkeypatch,
        {"id": "f1", "name": "clip.mp4", "media_type": "video", "duration": 6.0,
         "ai_metadata": dict(FAILED_WITH_HANDLES)},
        {"id": "c1", "file_id": "f1", "title": "clip", "start": 0.0, "end": 6.0},
    )
    assert panel.reindex_btn.setEnabled.call_args.args[0] is True

    panel.reindex()

    files_model.reindex_file.assert_called_once_with("f1")
