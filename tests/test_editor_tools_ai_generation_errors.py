"""Search, stock, indexing and URL-import tools: no crashes, and every failure starts with Error.

The backend answers "Error" by prefix only; anything else is read as success. These pin the
fixed failure paths of the pre-existing handlers (ai.search, ai.stock_media, indexing).
"""

import pytest

from classes import tool_handlers as th

L1 = 1000000


def _item(**fields):
    """The SearchItem objects _twelvelabs_search_in_window builds."""
    return type("SearchItem", (), fields)()


@pytest.fixture
def indexed_clip(editor, monkeypatch):
    """A clip whose source video is indexed; the backend and watch window are stubbed."""
    from unittest.mock import MagicMock

    from classes import api_client

    ai = {"analyzed": True, "index": {"status": "ready", "index_id": "idx", "video_id": "vid"},
          "scene_descriptions": [{"time": 15.0, "description": "a dog runs on the beach"}]}
    fid = editor.add_file("video", duration=60, ai_metadata=ai)
    cid = editor.add_clip(fid, position=0.0, layer=L1, start=10.0, end=40.0)
    client = MagicMock()
    client.is_indexing_configured.return_value = True
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)
    monkeypatch.setattr(th, "_lookup_watch_meta", lambda *a, **k: ("", 0.0, []))
    monkeypatch.setattr(th, "_apply_watch_to_match", lambda m, *a, **k: m)
    monkeypatch.setattr(th, "_watch_confirm_cut", lambda path, s, e, q, fallback=0.0, **k: {"cut_source": fallback})
    return editor, cid, fid, client


# --- search_clip_scenes ---------------------------------------------------------------------------

def test_search_clip_scenes_broad_fallback_handles_search_item_objects(indexed_clip, monkeypatch):
    """Regression: `it.get("video_id")` on SearchItem objects raised AttributeError."""
    editor, cid, _fid, _client = indexed_clip
    calls = []

    def search(index_id, query, page_limit=30, video_id=""):
        calls.append(video_id)
        if video_id:                        # narrow search: nothing
            return [], None
        return [_item(video_id="vid", start=12.0, end=18.0, rank=1),
                _item(video_id="other", start=1.0, end=2.0, rank=2)], None

    monkeypatch.setattr(th, "_tl_search_items_in_window", search)
    out = editor.call("search_clip_scenes_tool", query="surfer", timeline_clip_id=cid)
    assert calls == ["vid", ""]
    assert not out.startswith("Error"), out
    assert "(project search)" in out and "keep 0:02-0:08" in out


def test_search_clip_scenes_reports_a_failed_index_search_when_nothing_else_matches(indexed_clip, monkeypatch):
    editor, cid, _fid, _client = indexed_clip
    monkeypatch.setattr(th, "_tl_search_items_in_window", lambda *a, **k: ([], "503 Service Unavailable"))
    out = editor.call("search_clip_scenes_tool", query="volcano", timeline_clip_id=cid)
    assert out.startswith("Error: the index search failed (503 Service Unavailable)")


def test_search_clip_scenes_notes_when_the_answer_comes_from_scene_descriptions(indexed_clip, monkeypatch):
    editor, cid, _fid, client = indexed_clip
    client.is_indexing_configured.return_value = False
    out = editor.call("search_clip_scenes_tool", query="dog", timeline_clip_id=cid)
    assert out.startswith("Description matches")
    assert "Note: video indexing is not configured on the backend" in out
    none = editor.call("search_clip_scenes_tool", query="volcano", timeline_clip_id=cid)
    assert none.startswith("No matches found for 'volcano' in the scene descriptions")


# --- slice_clip_at_best_match -----------------------------------------------------------------------

def test_slice_at_best_match_without_a_match_is_an_error(indexed_clip, monkeypatch):
    editor, cid, _fid, _client = indexed_clip
    monkeypatch.setattr(th, "_twelvelabs_search_in_window", lambda *a, **k: ([], None))
    monkeypatch.setattr(th, "_scene_description_cut_source", lambda *a, **k: None)
    out = editor.call("slice_clip_at_best_match_tool", query="volcano", timeline_clip_id=cid)
    assert out.startswith("Error: No match for 'volcano' in this clip")
    assert editor.undo_steps_since_mark() == 0


def test_slice_at_best_match_outside_the_trim_is_an_error(indexed_clip, monkeypatch):
    editor, cid, _fid, _client = indexed_clip
    monkeypatch.setattr(th, "_twelvelabs_search_in_window",
                        lambda *a, **k: ([_item(video_id="vid", start=50.0, end=55.0, rank=1)], None))
    monkeypatch.setattr(th, "_scene_description_cut_source", lambda *a, **k: None)
    out = editor.call("slice_clip_at_best_match_tool", query="sunset", timeline_clip_id=cid)
    assert out.startswith("Error: The index found 'sunset' elsewhere")


@pytest.mark.parametrize("status, message", [("indexing", "still being indexed"), ("failed", "indexing failed")])
def test_slice_at_best_match_on_an_unready_index_is_an_error(editor, status, message):
    ai = {"analyzed": True, "index": {"status": status, "index_id": "idx", "video_id": "vid", "error": "boom"}}
    fid = editor.add_file("video", duration=60, ai_metadata=ai)
    cid = editor.add_clip(fid, position=0.0, layer=L1, end=30.0)
    out = editor.call("slice_clip_at_best_match_tool", query="dog", timeline_clip_id=cid)
    assert out.startswith("Error") and message in out


# --- stock ------------------------------------------------------------------------------------------

@pytest.fixture
def backend(monkeypatch):
    from unittest.mock import MagicMock

    from classes import api_client, credits_client
    client = MagicMock()
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)
    monkeypatch.setattr(credits_client, "check_operation", lambda *a, **k: (True, 100, None))
    monkeypatch.setattr(credits_client, "charge_operation_on_success", lambda *a, **k: None)
    return client


def test_stock_download_failures_are_errors(editor, backend):
    backend.pexels_download.return_value = {"error": "HTTP 404"}
    out = editor.call("import_stock_media_tool", source="pexels", link="https://example.invalid/v.mp4",
                      video_id="7")
    assert out == "Error: Pexels download failed: HTTP 404"
    backend.freesound_download.return_value = {"local_path": ""}
    out = editor.call("import_stock_media_tool", source="freesound", preview_url="https://x/p.mp3", sound_id="9")
    assert out == "Error: Freesound download failed: no file path"


def test_stock_credit_block_is_an_error(editor, backend, monkeypatch):
    from classes import credits_client
    monkeypatch.setattr(credits_client, "check_operation",
                        lambda *a, **k: (False, 0, "Insufficient credits (0 remaining, need 1 for stock_add)."))
    out = editor.call("import_stock_media_tool", source="pexels", link="https://example.invalid/v.mp4")
    assert out == "Error: Insufficient credits (0 remaining, need 1 for stock_add)."
    backend.pexels_download.assert_not_called()


def test_stock_import_never_opens_a_modal_for_a_bad_file(editor, tmp_path, monkeypatch):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    calls = []
    editor.window.files_model.add_files.side_effect = lambda files, **kw: calls.append(kw) or []
    out = editor.call("import_stock_media_tool", local_path=str(bad))
    assert calls == [{"quiet": True}]
    assert out.startswith("Error: Failed to import into project files")


# --- indexing ---------------------------------------------------------------------------------------

def test_reindex_not_configured_is_an_error(editor, backend):
    fid = editor.add_file("video", duration=30)
    backend.is_indexing_configured.return_value = False
    out = editor.call("reindex_project_file_tool", file_id=fid, force="true")
    assert out.startswith("Error: Gemini indexing is not configured")


def test_reindex_failure_is_an_error(editor, backend):
    fid = editor.add_file("video", duration=30)
    backend.is_indexing_configured.return_value = True
    backend.reindex_video.return_value = {"success": False, "error": "quota exceeded"}
    out = editor.call("reindex_project_file_tool", file_id=fid, force="true")
    assert out == "Error: Re-indexing failed: quota exceeded"


def test_wait_until_indexed_reports_pending_files_as_an_error(editor, monkeypatch):
    done = editor.add_file("video", duration=30)
    slow = editor.add_file("video", path="/media/slow.mp4", duration=30)
    monkeypatch.setattr(th, "_wait_for_file_indexing",
                        lambda fid, fm, timeout_sec=0, **_kw: "" if fid == done else "timed out after 30s")
    out = editor.call("wait_until_project_indexed_tool", timeout_seconds=30)
    assert out.startswith("Error: indexing did not finish for 1 of 2 project file(s) within 30s (1 indexed)")
    assert f"{slow} (timed out after 30s)" in out


def test_wait_for_one_file_respects_a_short_budget(monkeypatch):
    """A file waited on after the shared deadline no longer gets another 30 s."""
    import time
    from unittest.mock import MagicMock

    import classes.query as query
    files_model = MagicMock()
    files_model.is_file_indexing.return_value = True          # never finishes
    monkeypatch.setattr(query.File, "get", classmethod(lambda cls, **kw: None))
    started = time.monotonic()
    err = th._wait_for_file_indexing("f1", files_model, timeout_sec=1)
    assert err.startswith("timed out") and time.monotonic() - started < 5


def test_a_file_nothing_is_indexing_is_not_waited_on(monkeypatch):
    """Found live: with indexing skipped on import, the assistant sat out the whole wait (135 s)."""
    import time
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import classes.query as query
    files_model = MagicMock(_indexing_queue=[], _active_indexers=[])
    files_model.is_file_indexing.return_value = False
    monkeypatch.setattr(query.File, "get", classmethod(lambda cls, **kw: SimpleNamespace(data={"ai_metadata": {}})))
    started = time.monotonic()
    err = th._wait_for_file_indexing("f1", files_model, timeout_sec=60, idle_grace=0.2)
    assert err == th._NOT_BEING_INDEXED and time.monotonic() - started < 5


def test_wait_until_indexed_returns_at_once_when_nothing_is_indexing(editor, monkeypatch):
    a = editor.add_file("video", duration=30)
    b = editor.add_file("video", path="/media/b.mp4", duration=30)
    graces = []

    def waiter(fid, fm, timeout_sec=0, idle_grace=10.0):
        graces.append(idle_grace)
        return th._NOT_BEING_INDEXED

    monkeypatch.setattr(th, "_wait_for_file_indexing", waiter)
    out = editor.call("wait_until_project_indexed_tool", timeout_seconds=600)
    assert out.startswith("Error: 2 of 2 project file(s) are not indexed and nothing is indexing them")
    assert a in out and b in out and "reindex_project_file_tool" in out
    assert graces == [10.0, 1.0]


# --- import from a URL ----------------------------------------------------------------------------------

def test_import_video_url_checks_the_track_before_downloading(editor, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("downloaded"))
    out = editor.call("import_video_url_and_add_to_timeline_tool", video_url="https://example.invalid/a.mp4",
                      track="12")
    assert out.startswith("Error")
    editor.lock_track(L1)
    out = editor.call("import_video_url_and_add_to_timeline_tool", video_url="https://example.invalid/a.mp4",
                      track="1")
    assert out.startswith("Error: Track") and "locked" in out


def test_import_video_url_placement_failure_is_an_error(editor, monkeypatch, tmp_path):
    import io
    import urllib.request

    from classes.query import File

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(b"video-bytes"))

    def fake_import(path, preserve_alpha=None):
        f = File()
        f.data = {"path": "/media/url.mp4", "media_type": "video", "duration": 4.0}
        f.save()
        return f, None

    monkeypatch.setattr(th, "_import_generated_video", fake_import)
    monkeypatch.setattr(th, "add_clip_to_timeline", lambda **kw: "Error: duration_seconds needs a watch")
    out = editor.call("import_video_url_and_add_to_timeline_tool", video_url="https://example.invalid/a.mp4")
    assert out.startswith("Error: Video imported (file_id=") and "Do NOT download it again" in out



