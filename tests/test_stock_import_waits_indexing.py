"""Stock import waits for indexing (unit)."""

import os
import sys
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


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


def test_extract_chunks_image_no_ffmpeg(tmp_path):
    from classes.index_chunker import extract_chunks

    img = tmp_path / "x.png"
    img.write_bytes(b"PNG")
    infos, work, err = extract_chunks(str(img), [{"chunk_index": 0}], media_type="image")
    assert not err
    assert len(infos) == 1
    assert infos[0]["path"] == str(img)
    assert work == ""


@patch("classes.index_chunker._ffmpeg_run", return_value=(True, ""))
@patch("classes.index_chunker.os.path.isfile", return_value=True)
@patch("classes.index_chunker.os.path.getsize", return_value=50)
def test_extract_chunks_audio_uses_mp3(mock_size, mock_isfile, mock_ff, tmp_path):
    from classes.index_chunker import extract_chunks

    def _run(args):
        out = args[-1]
        open(out, "wb").write(b"mp3")
        assert "-vn" in args
        return True, ""

    mock_ff.side_effect = _run
    plan = [{"chunk_index": 0, "start": 0.0, "end": 10.0, "role": "audio"}]
    infos, work, err = extract_chunks(
        "in.wav", plan, out_dir=str(tmp_path / "w"), media_type="audio",
    )
    assert not err
    assert infos[0]["path"].endswith(".mp3")


def test_import_generated_uses_skip_indexing():
    import inspect
    from classes import tool_handlers as th
    src = inspect.getsource(th._import_generated_video)
    assert "skip_indexing=True" in src
