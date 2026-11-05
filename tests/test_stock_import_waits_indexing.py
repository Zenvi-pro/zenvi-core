"""Stock import waits for indexing (unit)."""

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


@pytest.fixture(autouse=True)
def _restore_query_module():
    """The fake classes.query must not outlive the test: later tests that use
    the real query layer (the editor-tools harness) would get the fake."""
    saved = sys.modules.get("classes.query")
    yield
    if saved is None:
        sys.modules.pop("classes.query", None)
    else:
        sys.modules["classes.query"] = saved


def _install_fake_query_module(fake_file):
    """Avoid importing real classes.query (pulls PyQt)."""
    mod = types.ModuleType("classes.query")

    class File:
        @staticmethod
        def get(**kwargs):
            return fake_file

        @staticmethod
        def filter(**kwargs):
            return [fake_file] if fake_file else []

    mod.File = File
    sys.modules["classes.query"] = mod
    return mod


def test_wait_for_file_indexing_ready():
    from classes.tool_handlers import _wait_for_file_indexing

    files_model = MagicMock()
    files_model.is_file_indexing.return_value = False
    files_model._indexing_queue = []
    files_model._active_indexers = []

    fake = MagicMock()
    fake.data = {
        "ai_metadata": {
            "analyzed": True,
            "index": {"status": "ready", "index_id": "z", "video_id": "f1"},
        }
    }
    _install_fake_query_module(fake)
    err = _wait_for_file_indexing("f1", files_model, timeout_sec=5)
    assert err == ""


def test_wait_for_file_indexing_failed():
    from classes.tool_handlers import _wait_for_file_indexing

    files_model = MagicMock()
    files_model.is_file_indexing.return_value = False
    files_model._indexing_queue = []
    files_model._active_indexers = []

    fake = MagicMock()
    fake.data = {
        "ai_metadata": {
            "index": {"status": "failed", "error": "boom"},
        }
    }
    _install_fake_query_module(fake)
    err = _wait_for_file_indexing("f1", files_model, timeout_sec=5)
    assert "boom" in err or "failed" in err


def test_get_media_type_mp3_with_has_video_true():
    from classes.image_types import get_media_type

    # libopenshot quirk: audio file flagged as having video
    assert get_media_type({
        "path": "C:/Users/x/Downloads/track.mp3",
        "has_video": True,
        "has_audio": True,
    }) == "audio"
    assert get_media_type({
        "path": "clip.mp4",
        "has_video": True,
        "has_audio": True,
    }) == "video"


def test_import_generated_uses_skip_indexing():
    import inspect
    from classes import tool_handlers as th
    src = inspect.getsource(th._import_generated_video)
    assert "skip_indexing=True" in src
