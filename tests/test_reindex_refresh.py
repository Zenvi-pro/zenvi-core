"""Re-indexing a file after a failed index re-runs it and refreshes what is shown (#143).

Headless. Indexing goes through the real ``IndexingJob`` (media index v2) on both of its
paths: on this computer (EmbeddingGemma search vectors, LFM3-Omni descriptions) and through
the backend (Gemini Embedding 2 search vectors, Gemini descriptions). Only the models are
fakes. The files model subclasses Qt types, so its real methods are compiled straight from
source (as test_indexing_queue.py does) and run against the headless editor (real project
store and undo history) with a worker that runs the job to the end when started. The Scene
Descriptions panel builds on the conftest Qt stub.
"""

import ast
import base64
import os
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from classes import credits_client as cc  # noqa: E402
from classes import info  # noqa: E402
from classes import local_describe as ld  # noqa: E402
from classes import query  # noqa: E402
from classes import tool_handlers as th  # noqa: E402
from classes.ai_metadata_utils import (  # noqa: E402
    get_effective_ai_metadata,
    materialize_clip_ai_metadata,
    merge_indexing_result,
)
from classes.api_client import ZenviBackendClient  # noqa: E402
from classes.indexing_status import FAILED, PENDING, SKIPPED, SUCCESS, derive_indexing_status  # noqa: E402
from classes.media_cache import save_ai_metadata  # noqa: E402
from classes.media_fingerprint import fingerprint  # noqa: E402
from classes.media_index import Shelf, cloud, embed_runtime, vectors  # noqa: E402
from classes.media_index import embedder as E  # noqa: E402
from classes.media_index import schema as S  # noqa: E402
from classes.media_index.job import IndexingJob  # noqa: E402

FILES_MODEL = os.path.join(SRC, "windows", "models", "files_model.py")

READY = {"status": "ready", "index_id": "zenvi-p1", "video_id": "f1", "provider": "media-index-v2"}

# Analysis with descriptions, as an earlier index left it.
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

# What the job reports for a file it indexed for search but nothing has described yet
# (descriptions on import are off, and the footage is silent).
SEARCH_ONLY = {"analyzed": True, "provider": "media-index-v2", "short_summary": "", "description": "",
               "transcript": "", "chapters": [], "scene_descriptions": [], "transcript_cues": [], "tags": {},
               "index": dict(READY), "twelvelabs": dict(READY)}

SEARCH_FAILED = "Could not index this file for search: "


# ── what a file holds after an attempt ─────────────────────────────────────


def test_a_usable_retry_replaces_the_failed_state_and_is_dated():
    merged = merge_indexing_result(dict(FRESH, error="first failure"), dict(FRESH, description="second run"))

    assert "error" not in merged
    assert merged["description"] == "second run"
    assert merged["analysis_date"]
    assert derive_indexing_status(merged).state == SUCCESS


def test_a_search_only_success_after_a_failure_clears_the_failure_and_keeps_the_analysis():
    """The #143 case main still had: the earlier error outlived a retry that succeeded."""
    merged = merge_indexing_result(dict(FRESH, error="first failure"), dict(SEARCH_ONLY))

    assert "error" not in merged
    assert merged["description"] == FRESH["description"]
    assert derive_indexing_status(merged).state == SUCCESS


def test_a_failed_retry_keeps_good_content_but_reports_its_own_error():
    merged = merge_indexing_result(
        dict(FRESH, error="first failure"), {"analyzed": False, "error": "second failure", "index": {"status": "failed"}},
    )

    assert merged["description"] == FRESH["description"]
    st = derive_indexing_status(merged)
    assert (st.state, st.tooltip) == (FAILED, "second failure")


def test_a_failure_reported_only_on_its_index_block_replaces_the_old_error():
    merged = merge_indexing_result(dict(FRESH, error="first failure"),
                                   {"index": {"status": "failed", "error": "upload rejected"}})

    st = derive_indexing_status(merged)
    assert (st.state, st.tooltip) == (FAILED, "upload rejected")


def test_failure_then_failure_shows_the_new_error():
    first = merge_indexing_result(None, {"analyzed": False, "error": "first failure"})
    second = merge_indexing_result(first, {"analyzed": False, "error": "second failure"})

    assert derive_indexing_status(second).tooltip == "second failure"


# ── the indexing job, through the files model, on the headless editor ──────


class _Signal:
    """A Qt signal that calls its slots in place and keeps what it sent."""

    def __init__(self):
        self.slots, self.emitted = [], []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        self.emitted.append(args)
        for slot in list(self.slots):
            slot(*args)


_MODEL_METHODS = [
    "_apply_ai_metadata", "_save_file_untracked", "_set_indexing_progress", "file_indexing_status",
    "is_file_indexing", "is_file_queued", "get_indexing_progress", "_enqueue_index", "_timeline_file_ids",
    "_drain_indexing_queue", "_index_file_async", "_start_indexing_worker", "offers_reindex", "can_reindex_file",
    "reindex_file",
]


def _compile_files_model(**module_globals):
    """The real FilesModel methods, compiled without importing Qt."""
    with open(FILES_MODEL, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FilesModel")
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in _MODEL_METHODS]
    ns = dict(module_globals)
    exec(compile(ast.Module(body=funcs, type_ignores=[]), FILES_MODEL, "exec"), ns)
    return type("FilesModelMethods", (), {n: ns[n] for n in _MODEL_METHODS})()


class _LocalSearchModel:
    """EmbeddingGemma 2 on this computer (its Node worker is the part faked)."""

    model = E.MODEL_LOCAL

    def __init__(self, world):
        self.world = world

    def texts(self, texts, query=False):
        if self.world.search_crash:
            # e.g. the runtime folder is not writable: not an EmbedError, so the job itself fails
            raise PermissionError(self.world.search_crash)
        if self.world.search_failure:
            raise E.EmbedError(self.world.search_failure)
        return [np.ones(4, np.float32) / 2 for _ in texts]


class World:
    """One video in a project, indexed by the real job through the real files model.

    *path* "local" is the default setup: nobody signed in, search vectors and descriptions made
    on this computer. "backend" is a signed-in user who picked the cloud search model and
    Gemini descriptions, so both go through the Zenvi backend client.
    """

    DESCRIPTION = {"local": "A dog runs on a beach (LFM3-Omni).", "backend": "A dog runs on a beach (Gemini)."}

    def __init__(self, editor, monkeypatch, tmp_path, *, path="local", describe=True, ai_metadata=None,
                 with_fingerprint=True):
        self.editor, self.path = editor, path
        self.search_failure, self.search_flags, self.search_crash = "", {}, ""
        self.describe_failure = ""
        self.describers = []

        editor.settings[E.DESCRIBE_SETTING] = describe
        if path == "backend":
            editor.settings[E.SETTING] = "cloud"
            editor.settings["index-describe"] = "gemini"
        monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: path == "backend"))
        monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (True, 1000, None))
        self.charges = []
        monkeypatch.setattr(cc, "charge_operation_on_success", lambda *a, **k: self.charges.append(a))

        self.shelf = Shelf(str(tmp_path / "shelf"))
        for target in ("classes.media_index.store.default_shelf", "classes.media_index.default_shelf",
                       "classes.editor_tools.media_index_tools.default_shelf"):
            monkeypatch.setattr(target, lambda: self.shelf)
        media = tmp_path / "clip.mp4"
        media.write_bytes(os.urandom(4096))
        self.sha = fingerprint(str(media))["sha256"]

        def facts(file_path, **kw):
            """The local layers: two shots, colour, sound and speech analysed."""
            self.shelf.write_json(self.sha, "structure.json",
                                  {"shots": [{"id": 0, "start": 0.0, "end": 3.0}, {"id": 1, "start": 3.0, "end": 6.0}]})
            for layer in (S.LAYER_STRUCTURE, S.LAYER_LOOK, S.LAYER_AUDIO, S.LAYER_SPEECH):
                self.shelf.set_layer(self.sha, layer, version=S.LAYER_VERSIONS[layer], status="ready")
            return {"ok": True}

        monkeypatch.setattr("classes.media_index.facts.compute_facts", facts)
        monkeypatch.setattr("classes.media_index.probe.probe_media", lambda p: {"has_video": True, "duration": 6.0})
        monkeypatch.setattr(embed_runtime, "needs_download", lambda: False)

        def compute_vectors(file_path, probe, sha, shelf, embedder, **kw):
            vecs = embedder.texts(["shot 0"])
            vectors.write_layer(shelf, sha, [{"kind": "text", "shot": 0, "t": 1.0}], vecs, model=embedder.model)
            return {"status": "ready", "rows": 1}

        monkeypatch.setattr(vectors, "compute_vectors", compute_vectors)
        monkeypatch.setattr(vectors, "add_descriptions", lambda *a, **k: 0)

        self.client = MagicMock()
        self.client._empty_ai_metadata.side_effect = ZenviBackendClient._empty_ai_metadata

        def v2_embed(items, dims=768, task_type=None, session=None):
            if self.search_failure:
                return {"error": self.search_failure, **self.search_flags}
            return {"vectors": [base64.b64encode(np.ones(4, "<f2").tobytes()).decode() for _ in items]}

        self.client.v2_embed.side_effect = v2_embed
        local = _LocalSearchModel(self)
        monkeypatch.setattr(E, "get_embedder",
                            lambda model: E.CloudEmbedder(self.client) if model == E.MODEL_CLOUD else local)

        monkeypatch.setattr(ld, "ready", lambda: True)

        def compute_cloud(client, file_path, probe, sha, shelf, **kw):
            """Describe the shots: LFM3-Omni here, or Gemini through the backend, as the job chose."""
            self.describers.append("LFM3-Omni" if ld.enabled() else "Gemini")
            if self.describe_failure:
                return {"error": self.describe_failure}
            text = self.DESCRIPTION["local" if ld.enabled() else "backend"]
            structure = shelf.read_json(sha, "structure.json") or {}
            watch = {"shots": [dict(s, description=text) for s in structure.get("shots") or []]}
            shelf.write_json(sha, "watch.json", watch)
            return {"layers": {S.LAYER_WATCH: "ready"}, "watch": watch}

        monkeypatch.setattr(cloud, "compute_cloud", compute_cloud)

        data = {"ai_metadata": dict(ai_metadata or {})}
        if with_fingerprint:
            data["fingerprint"] = fingerprint(str(media))
        self.fid = editor.add_file("video", path=str(media), duration=6.0, **data)

        client = self.client

        class SyncWorker:
            """BackendIndexingWorker without the thread: start() runs the real job to the end."""

            def __init__(self, file_data, project_id="", parent=None):
                self.file_data, self.project_id = file_data, project_id
                self.progress, self.completed, self.finished = _Signal(), _Signal(), _Signal()

            def start(self):
                IndexingJob(self.file_data, self.project_id, client_factory=lambda: client,
                            emit_completed=self.completed.emit, emit_progress=self.progress.emit).run()
                self.finished.emit()

        self.cache_writes = []
        monkeypatch.setattr("classes.media_cache.save_ai_metadata",
                            lambda fp, meta: self.cache_writes.append(dict(meta)) or True)
        editor.window.FileUpdated = _Signal()
        model = _compile_files_model(File=query.File, get_app=lambda: editor.app, log=MagicMock(),
                                     BackendIndexingWorker=SyncWorker)
        model._active_indexers, model._indexing_queue = [], []
        model._indexing_progress, model._status_cache = {}, {}
        model._MAX_INDEXING_WORKERS = 2
        model.indexingProgress = _Signal()
        editor.window.files_model = self.model = model

    @property
    def meta(self):
        return self.editor.file(self.fid)["ai_metadata"]

    def badge(self):
        return self.model.file_indexing_status(self.fid)

    def file_updates(self):
        return [args for args in self.editor.window.FileUpdated.emitted if args == (self.fid,)]


@pytest.fixture(params=["local", "backend"])
def indexing_path(request):
    return request.param


def _fail_search(world, reason):
    world.search_failure = reason
    if world.path == "backend":
        world.search_flags = {}


def test_failure_then_reindex_success_shows_the_new_analysis(editor, monkeypatch, tmp_path, indexing_path):
    w = World(editor, monkeypatch, tmp_path, path=indexing_path)
    _fail_search(w, "the search model did not start")
    w.model._index_file_async(w.fid)  # the import
    st = w.badge()
    assert (st.state, st.tooltip) == (FAILED, SEARCH_FAILED + "the search model did not start")

    w.search_failure = ""
    w.model.reindex_file(w.fid)

    assert w.badge().state == SUCCESS
    assert "error" not in w.meta
    assert World.DESCRIPTION[indexing_path] in w.meta["description"]
    assert w.describers == ["LFM3-Omni" if indexing_path == "local" else "Gemini"]
    assert len(w.file_updates()) == 2  # the panel and the badges repaint after each attempt
    assert editor.undo_steps_since_mark() == 0  # background results are not the user's edits
    assert w.charges == []


def test_failure_then_failure_shows_the_second_error(editor, monkeypatch, tmp_path, indexing_path):
    w = World(editor, monkeypatch, tmp_path, path=indexing_path)
    _fail_search(w, "first failure")
    w.model._index_file_async(w.fid)
    _fail_search(w, "second failure")
    w.model.reindex_file(w.fid)

    st = w.badge()
    assert (st.state, st.tooltip) == (FAILED, SEARCH_FAILED + "second failure")
    assert editor.undo_steps_since_mark() == 0


def test_a_search_only_retry_after_a_failure_clears_it_and_keeps_the_earlier_analysis(
        editor, monkeypatch, tmp_path, indexing_path):
    """Descriptions on import are off, so the retry brings no description: the earlier one stays."""
    w = World(editor, monkeypatch, tmp_path, path=indexing_path, describe=False, ai_metadata=FRESH)
    _fail_search(w, "the search model did not start")
    w.model._index_file_async(w.fid)
    assert w.badge().state == FAILED
    assert w.meta["description"] == FRESH["description"]
    assert w.cache_writes == []  # a failure has nothing new to cache

    w.search_failure = ""
    w.model.reindex_file(w.fid)

    assert w.badge().state == SUCCESS
    assert "error" not in w.meta
    assert w.meta["description"] == FRESH["description"]
    assert w.meta["index"]["status"] == "ready"


def test_out_of_credits_on_the_backend_is_a_skip_and_a_retry_clears_it(editor, monkeypatch, tmp_path):
    w = World(editor, monkeypatch, tmp_path, path="backend")
    w.search_failure, w.search_flags = "Not enough credits", {"credits": True}
    w.model._index_file_async(w.fid)
    st = w.badge()
    assert (st.state, st.tooltip) == (SKIPPED, "Not enough credits")

    w.search_failure, w.search_flags = "", {}
    w.model.reindex_file(w.fid)

    assert w.badge().state == SUCCESS
    assert World.DESCRIPTION["backend"] in w.meta["description"]


def test_a_failed_description_pass_is_described_by_reindex(editor, monkeypatch, tmp_path, indexing_path):
    """Describing never fails the index (search works without it); Re-index describes what is missing."""
    w = World(editor, monkeypatch, tmp_path, path=indexing_path)
    w.describe_failure = "the describer stopped"
    w.model._index_file_async(w.fid)
    assert w.badge().state == SUCCESS
    assert not w.meta["description"]

    w.describe_failure = ""
    w.model.reindex_file(w.fid)

    assert World.DESCRIPTION[indexing_path] in w.meta["description"]
    assert len(w.describers) == 2


def test_a_fresh_result_is_cached_and_dated(editor, monkeypatch, tmp_path):
    w = World(editor, monkeypatch, tmp_path)
    w.model._index_file_async(w.fid)

    assert w.meta["analysis_date"]
    assert len(w.cache_writes) == 1 and World.DESCRIPTION["local"] in w.cache_writes[0]["description"]


# ── the Re-index action ────────────────────────────────────────────────────


def test_reindex_queues_one_run_and_shows_it_waiting(editor, monkeypatch, tmp_path):
    w = World(editor, monkeypatch, tmp_path)
    w.model._MAX_INDEXING_WORKERS = 0  # every worker slot is busy

    w.model.reindex_file(w.fid)
    w.model.reindex_file(w.fid)  # already queued: not queued twice

    assert w.model._indexing_queue == [w.fid]
    assert w.badge().state == PENDING
    assert w.model.can_reindex_file(w.fid) is False


def test_reindex_is_not_offered_while_indexing_or_for_files_that_are_never_indexed(editor, monkeypatch, tmp_path):
    w = World(editor, monkeypatch, tmp_path)
    w.model._active_indexers = [types.SimpleNamespace(file_data={"id": w.fid})]

    assert w.model.can_reindex_file(w.fid) is False
    assert w.model.can_reindex_file("missing") is False
    w.model.reindex_file(w.fid)
    assert w.model._indexing_queue == []


def test_a_title_has_no_reindex(editor, monkeypatch, tmp_path):
    """Found in the app: a title the Title tools wrote got Re-index. It is an SVG imported as an image."""
    w = World(editor, monkeypatch, tmp_path)
    title = editor.add_file("image", path=str(tmp_path / "title" / "Re-index test.svg"))
    picture = editor.add_file("image", path=str(tmp_path / "still.png"))
    assert query.File.get(id=title).data["media_type"] == "image"

    assert w.model.offers_reindex(query.File.get(id=title).data) is False
    assert w.model.can_reindex_file(title) is False
    w.model.reindex_file(title)
    assert w.model._indexing_queue == [] and w.model._active_indexers == []
    assert w.model.offers_reindex(query.File.get(id=picture).data) is True


# ── reindex_project_file_tool ──────────────────────────────────────────────


def _call_tool(monkeypatch, file_id):
    # No Qt event loop runs here: run the main-thread hop inline.
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, timeout=None: fn(*a))
    return th.reindex_project_file(file_id=file_id)


def test_the_tool_reruns_a_failed_index_even_when_every_layer_is_saved(editor, monkeypatch, tmp_path):
    """The failure predates the layers (here: another run saved them since). main said "nothing to re-index"."""
    w = World(editor, monkeypatch, tmp_path)
    w.model._index_file_async(w.fid)
    stale = dict(w.meta, error="Description generation failed")
    editor.app.updates.update_untracked(["files", {"id": w.fid}], {"ai_metadata": stale})
    assert w.badge().state == FAILED

    out = _call_tool(monkeypatch, w.fid)

    assert out.startswith(f"File {w.fid} queued for indexing (last index failed: Description generation failed)"), out
    assert w.badge().state == SUCCESS
    assert "error" not in w.meta


def test_the_tool_reruns_a_file_that_failed_for_want_of_a_fingerprint(editor, monkeypatch, tmp_path):
    block = {"status": "failed", "error": "The file has no fingerprint, so it cannot be indexed."}
    w = World(editor, monkeypatch, tmp_path, with_fingerprint=False,
              ai_metadata={"analyzed": False, "error": block["error"], "index": block})

    out = _call_tool(monkeypatch, w.fid)

    assert "queued for indexing" in out, out
    assert w.badge().state == SUCCESS
    assert World.DESCRIPTION["local"] in w.meta["description"]


def test_the_tool_leaves_a_clean_complete_file_alone(editor, monkeypatch, tmp_path):
    w = World(editor, monkeypatch, tmp_path)
    w.model._index_file_async(w.fid)
    describers = list(w.describers)

    out = _call_tool(monkeypatch, w.fid)

    assert "nothing to re-index" in out
    assert w.describers == describers


@pytest.mark.parametrize("file_id", ["", "missing"])
def test_the_tool_reports_a_bad_file_id_as_an_error(editor, monkeypatch, tmp_path, file_id):
    World(editor, monkeypatch, tmp_path)
    assert _call_tool(monkeypatch, file_id).startswith("Error:")


# ── timeline clips and the fingerprint cache ───────────────────────────────


def _dated(meta, date, description):
    scenes = [{"description": description + " scene", "source_time": 2.0, "time": 2.0}]
    return dict(meta, analysis_date=date, description=description, scene_descriptions=scenes)


def test_a_clip_snapshot_from_before_a_reindex_is_recomputed():
    old_root = _dated(FRESH, "2026-09-01T10:00:00+00:00", "old run")
    new_root = _dated(FRESH, "2026-09-30T10:00:00+00:00", "new run")
    # Not 0-based: clip_metadata_is_valid reads a 0.0 start as -1 and always recomputes.
    clip = {"start": 1.0, "end": 6.0, "ai_metadata": materialize_clip_ai_metadata(old_root, 1.0, 6.0)}

    effective = get_effective_ai_metadata({"ai_metadata": new_root}, clip, clip_ai_metadata=clip["ai_metadata"])

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

    effective = get_effective_ai_metadata({"fingerprint": fp, "ai_metadata": retried}, {"start": 0.0, "end": 6.0})

    assert effective["transcript"] == ""
    assert effective["transcript_cues"] == []


def test_a_file_that_only_holds_index_handles_is_still_filled_from_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    fp = {"sha256": "ef" * 32}
    save_ai_metadata(fp, dict(FRESH))

    effective = get_effective_ai_metadata({"fingerprint": fp, "ai_metadata": {"index": dict(READY)}},
                                          {"start": 0.0, "end": 6.0})

    assert effective["description"] == FRESH["description"]


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


def _panel(world, monkeypatch, *, timeline_clip=None):
    """The Scene Descriptions panel over the world's project, showing its file or one timeline clip."""
    from windows import ai_media_panel

    editor, model = world.editor, world.model
    model.selection_model = MagicMock()
    model.current_file = lambda: query.File.get(id=world.fid)
    editor.window.filesView.hasFocus.return_value = timeline_clip is None
    editor.window.selected_clips = [timeline_clip] if timeline_clip else []
    monkeypatch.setattr(ai_media_panel, "get_app", lambda: editor.app)
    # The stub's QTimer is a bare MagicMock, which would take the panel as its spec.
    monkeypatch.setattr(ai_media_panel, "QTimer", lambda *a, **k: MagicMock())
    return _stubbed(ai_media_panel.AIMediaPanel)()


def _body(panel):
    return panel.description_view.setPlainText.call_args.args[0]


def _status_line(panel):
    return panel.indexing_status_label.setText.call_args.args[0]


@needs_qt_stub
def test_the_panel_shows_the_new_description_once_a_reindex_succeeds(editor, monkeypatch, tmp_path, indexing_path):
    w = World(editor, monkeypatch, tmp_path, path=indexing_path)
    _fail_search(w, "first failure")
    w.model._index_file_async(w.fid)
    panel = _panel(w, monkeypatch)
    assert _body(panel) == SEARCH_FAILED + "first failure"
    assert _status_line(panel) == SEARCH_FAILED + "first failure"
    assert panel.reindex_btn.setEnabled.call_args.args[0] is True

    w.search_failure = ""
    panel.indexing_status_label.reset_mock()
    panel.reindex()  # the panel's Re-index button; no reselection afterwards

    assert World.DESCRIPTION[indexing_path] in _body(panel)
    panel.indexing_status_label.hide.assert_called()
    assert panel.reindex_btn.setEnabled.call_args.args[0] is True


@needs_qt_stub
def test_the_panel_shows_the_second_error_after_failure_then_failure(editor, monkeypatch, tmp_path):
    w = World(editor, monkeypatch, tmp_path)
    _fail_search(w, "first failure")
    w.model._index_file_async(w.fid)
    panel = _panel(w, monkeypatch)

    _fail_search(w, "second failure")
    panel.reindex()

    assert _body(panel) == SEARCH_FAILED + "second failure"
    assert _status_line(panel) == SEARCH_FAILED + "second failure"


@needs_qt_stub
def test_the_reindex_button_is_greyed_while_the_file_waits_for_a_worker(editor, monkeypatch, tmp_path):
    w = World(editor, monkeypatch, tmp_path)
    panel = _panel(w, monkeypatch)
    w.model._MAX_INDEXING_WORKERS = 0

    panel.reindex()

    assert panel.reindex_btn.setEnabled.call_args.args[0] is False
    assert _status_line(panel) == "Waiting to index"


@needs_qt_stub
def test_a_timeline_clip_shows_its_reindexed_source_not_its_saved_snapshot(editor, monkeypatch, tmp_path):
    old_root = _dated(FRESH, "2026-09-01T10:00:00+00:00", "old run")
    w = World(editor, monkeypatch, tmp_path, ai_metadata=old_root)
    clip = editor.add_clip(w.fid, start=0.0, end=6.0, ai_metadata=materialize_clip_ai_metadata(old_root, 0.0, 6.0))
    panel = _panel(w, monkeypatch, timeline_clip=clip)
    assert "old run" in _body(panel)

    panel.reindex()  # re-indexes the clip's source file

    assert World.DESCRIPTION["local"] in _body(panel)
    assert "old run" not in _body(panel)


@needs_qt_stub
def test_a_timeline_clip_keeps_its_snapshot_when_its_source_has_no_analysis(editor, monkeypatch, tmp_path):
    snapshot = materialize_clip_ai_metadata(_dated(FRESH, "2026-09-01T10:00:00+00:00", "old run"), 0.0, 6.0)
    w = World(editor, monkeypatch, tmp_path)
    clip = editor.add_clip(w.fid, start=0.0, end=6.0, ai_metadata=snapshot)

    panel = _panel(w, monkeypatch, timeline_clip=clip)

    assert "old run" in _body(panel)


@needs_qt_stub
def test_a_timeline_clip_reports_a_failed_retry_of_its_source(editor, monkeypatch, tmp_path):
    """The clip's analysis is still good (the failed retry kept it), but its source's badge says failed."""
    w = World(editor, monkeypatch, tmp_path, ai_metadata=FRESH)
    clip = editor.add_clip(w.fid, start=0.0, end=6.0, ai_metadata=materialize_clip_ai_metadata(FRESH, 0.0, 6.0))
    panel = _panel(w, monkeypatch, timeline_clip=clip)
    w.search_crash = "the search model folder is not writable"

    panel.reindex()

    assert w.badge().tooltip == "the search model folder is not writable"
    assert _status_line(panel) == "the search model folder is not writable"
    assert FRESH["description"] in _body(panel)
