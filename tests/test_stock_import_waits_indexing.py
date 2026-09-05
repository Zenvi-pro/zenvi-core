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
@patch("classes.index_chunker._probe_duration", return_value=400.0)
def test_extract_chunks_audio_slices_long_file(mock_dur, mock_size, mock_isfile, mock_ff, tmp_path):
    from classes.index_chunker import extract_chunks

    def _run(args):
        out = args[-1]
        open(out, "wb").write(b"aud")
        assert "-vn" in args
        return True, ""

    mock_ff.side_effect = _run
    plan = [{"chunk_index": 0, "start": 0.0, "end": 180.0, "role": "audio"}]
    infos, work, err = extract_chunks(
        "in.wav", plan, out_dir=str(tmp_path / "w"), media_type="audio",
    )
    assert not err
    assert len(infos) == 1
    assert infos[0]["owned"] is True
    assert mock_ff.called


@patch("classes.index_chunker._probe_duration", return_value=120.0)
@patch("classes.index_chunker.os.path.isfile", return_value=True)
@patch("classes.index_chunker.os.path.getsize", return_value=999)
def test_extract_chunks_audio_full_file_skips_ffmpeg(mock_size, mock_isfile, mock_dur, tmp_path):
    from classes.index_chunker import extract_chunks

    plan = [{"chunk_index": 0, "start": 0.0, "end": 120.0, "role": "audio"}]
    with patch("classes.index_chunker._ffmpeg_run") as mock_ff:
        infos, work, err = extract_chunks(
            "song.mp3", plan, out_dir=str(tmp_path / "w"), media_type="audio",
        )
        assert not mock_ff.called
    assert not err
    assert len(infos) == 1
    assert infos[0]["path"] == "song.mp3"
    assert infos[0]["owned"] is False
    assert work == ""


@patch("classes.index_chunker._probe_duration", return_value=120.0)
@patch("classes.index_chunker.os.path.isfile", return_value=True)
@patch("classes.index_chunker.os.path.getsize", return_value=999)
def test_extract_chunks_mp3_mislabeled_as_video_skips_ffmpeg(mock_size, mock_isfile, mock_dur, tmp_path):
    """MP3 imported as media_type=video must not take the libx264→mp4 path."""
    from classes.index_chunker import extract_chunks

    plan = [{"chunk_index": 0, "start": 0.0, "end": 120.0, "role": "clip"}]
    with patch("classes.index_chunker._ffmpeg_run") as mock_ff:
        infos, work, err = extract_chunks(
            "song.mp3", plan, out_dir=str(tmp_path / "w"), media_type="video",
        )
        assert not mock_ff.called
    assert not err
    assert infos[0]["mime_type"] in ("audio/mpeg", "audio/mp3")
    assert infos[0]["owned"] is False


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


def test_short_ffmpeg_error_strips_banner():
    from classes.index_chunker import _short_ffmpeg_error

    raw = (
        "ffmpeg version 8.1 Copyright (c) 2000-2026\n"
        "  built with gcc\n"
        "  libavutil 60\n"
        "Input #0, mp3, from 'x.mp3':\n"
        "Error while opening encoder for output stream\n"
    )
    msg = _short_ffmpeg_error(raw)
    assert "ffmpeg version" not in msg.lower()
    assert "Error while opening" in msg


def test_import_generated_uses_skip_indexing():
    import inspect
    from classes import tool_handlers as th
    src = inspect.getsource(th._import_generated_video)
    assert "skip_indexing=True" in src
