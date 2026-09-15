"""Global media cache: thumbnails, waveforms, AI metadata, eviction."""

import json
import os
import time

from classes import info
from classes.media_cache import (
    cache_size_bytes,
    entry_dir,
    evict_if_needed,
    load_ai_metadata,
    load_waveform,
    media_cache_root,
    preferred_thumbnail_path,
    resolve_thumbnail_path,
    save_ai_metadata,
    save_waveform,
)


def test_preferred_and_resolve_legacy_layouts(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "THUMBNAIL_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    (tmp_path / "thumbs").mkdir()
    file_id = "ABC123"
    # Legacy frame-1 layout
    legacy = tmp_path / "thumbs" / f"{file_id}.png"
    legacy.write_bytes(b"png")
    assert resolve_thumbnail_path(file_id, 1) == str(legacy)

    # Subdir layout takes precedence over legacy when both exist? Preferred writes subdir.
    preferred = preferred_thumbnail_path(file_id, 1)
    assert preferred.endswith(os.path.join(file_id, "1.png"))


def test_fingerprint_cache_shared_across_lookups(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr(info, "THUMBNAIL_PATH", str(tmp_path / "thumbs"))
    fp = {"sha256": "abc" * 10, "size": 1, "mtime": 1.0}
    write_path = preferred_thumbnail_path("fid", 3, fingerprint=fp)
    os.makedirs(os.path.dirname(write_path), exist_ok=True)
    open(write_path, "wb").write(b"frame")
    found = resolve_thumbnail_path("fid", 3, fingerprint=fp)
    assert found == write_path
    # Same fingerprint, different file_id still finds it
    found2 = resolve_thumbnail_path("other", 3, fingerprint=fp)
    assert found2 == write_path


def test_waveform_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    fp = {"sha256": "wave" * 8, "size": 10, "mtime": 1.0}
    samples = [0.1, 0.2, 0.3]
    assert save_waveform(fp, samples)
    loaded = load_waveform(fp)
    assert loaded["audio_data"] == samples


def test_ai_metadata_round_trip_strips_index_handles(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    fp = {"sha256": "meta" * 8, "size": 10, "mtime": 1.0}
    payload = {
        "analyzed": True,
        "transcript": "hello",
        "scene_descriptions": [{"description": "a", "source_time": 0.0}],
        "index": {"index_id": "keep-in-project"},
        "twelvelabs": {"video_id": "v1"},
    }
    assert save_ai_metadata(fp, payload)
    loaded = load_ai_metadata(fp)
    assert loaded["transcript"] == "hello"
    assert "index" not in loaded
    assert "twelvelabs" not in loaded


def test_evict_if_needed_removes_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "CACHE_PATH", str(tmp_path / "cache"))
    root = media_cache_root()
    old = os.path.join(root, "oldentry")
    new = os.path.join(root, "newentry")
    os.makedirs(old)
    os.makedirs(new)
    with open(os.path.join(old, "big.bin"), "wb") as fh:
        fh.write(b"x" * 5000)
    with open(os.path.join(new, "small.bin"), "wb") as fh:
        fh.write(b"y" * 100)
    # Make old older
    old_time = time.time() - 1000
    os.utime(old, (old_time, old_time))
    removed = evict_if_needed(limit_bytes=1000)
    assert removed >= 1
    assert not os.path.isdir(old)
    assert os.path.isdir(new)


def test_collect_and_reclaim(tmp_path, monkeypatch):
    from classes.media_collect import collect_media_into_project, reclaim_unused_asset_media
    from classes import info as info_mod

    monkeypatch.setattr(info_mod, "PATH", str(tmp_path / "app"))
    src = tmp_path / "Downloads" / "clip.mp4"
    src.parent.mkdir()
    src.write_bytes(b"body")
    project = str(tmp_path / "Proj.zvn")
    files = [{"id": "f1", "path": str(src), "original_path": str(src)}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": str(src)}}]

    copied, skipped, errors = collect_media_into_project(files, clips, project)
    assert len(copied) == 1
    assert files[0]["path"] != str(src)
    assert os.path.isfile(files[0]["path"])
    assert os.path.isfile(str(src))
    assert clips[0]["reader"]["path"] == files[0]["path"]

    # Reclaim should move path back to original when fingerprints match.
    removed, kept, errors = reclaim_unused_asset_media(files, project)
    assert len(removed) == 1
    assert files[0]["path"] == str(src)
