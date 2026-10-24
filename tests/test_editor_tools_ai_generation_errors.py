"""Stock, indexing and URL-import tools: no crashes, and every failure starts with Error.

The backend answers "Error" by prefix only; anything else is read as success. These pin the
fixed failure paths of the pre-existing handlers (ai.stock_media, indexing).
"""

import pytest

from classes import tool_handlers as th

L1 = 1000000


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
    out = th.import_video_url_and_add_to_timeline(video_url="https://example.invalid/a.mp4", track="12")
    assert out.startswith("Error")
    editor.lock_track(L1)
    out = th.import_video_url_and_add_to_timeline(video_url="https://example.invalid/a.mp4", track="1")
    assert out.startswith("Error: Track") and "locked" in out


def test_import_video_url_placement_failure_is_an_error(editor, monkeypatch, tmp_path):
    import io

    from classes.query import File

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("classes.web_video_ingest.open_public_url", lambda *a, **k: _Resp(b"video-bytes"))

    def fake_import(path, preserve_alpha=None):
        f = File()
        f.data = {"path": "/media/url.mp4", "media_type": "video", "duration": 4.0}
        f.save()
        return f, None

    monkeypatch.setattr(th, "_import_generated_video", fake_import)
    monkeypatch.setattr(th, "add_clip_to_timeline", lambda **kw: "Error: duration_seconds needs a watch")
    out = th.import_video_url_and_add_to_timeline(video_url="https://example.invalid/a.mp4")
    assert out.startswith("Error: Video imported (file_id=") and "Do NOT download it again" in out


# --- HyperFrames partial import ------------------------------------------------------------------------

def test_partial_motion_graphics_import_is_an_error(editor, monkeypatch):
    monkeypatch.setattr(th, "_MG_IMPORTED_URLS", {})
    results = iter([("F1", 1.0, None, False, "yuv420p"), (None, 0.0, "HTTP 500", False, "")])
    monkeypatch.setattr(th, "_download_and_import_one", lambda url, label="", job_transparent=None: next(results))
    out = th.fetch_motion_graphics_video(segment_urls=["https://x/segment_01.mp4", "https://x/segment_02.mp4"],
                                         label="Launch title")
    assert out.startswith("Error: Imported only 1/2 motion segments; 1 failed.")
    assert "HTTP 500" in out
