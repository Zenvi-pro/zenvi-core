"""Generated media is born durable — never under the OS temp directory."""

import os
import tempfile

import pytest

from classes import info
from classes.assets import cleanup_scratch_parent, durable_media_path


def test_durable_media_path_uses_project_assets_when_saved(tmp_path):
    project = str(tmp_path / "MyProject.zvn")
    (tmp_path / "MyProject.zvn").write_text("{}")
    path = durable_media_path(ext=".mp4", project_file_path=project)
    assert path.endswith(".mp4")
    assert "MyProject_assets" in path
    assert os.path.basename(os.path.dirname(path)) == "media"
    assert os.path.isdir(os.path.dirname(path))


def test_durable_media_path_uses_user_generated_when_unsaved(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    path = durable_media_path(ext=".webm", project_file_path=None)
    assert path.endswith(".webm")
    assert os.path.basename(os.path.dirname(path)) == "generated"
    assert str(tmp_path / "user") in path


def test_durable_media_path_normalizes_extension(tmp_path):
    project = str(tmp_path / "P.zvn")
    (tmp_path / "P.zvn").write_text("{}")
    path = durable_media_path(ext="mov", project_file_path=project)
    assert path.endswith(".mov")


def test_cleanup_scratch_parent_removes_matching_tmpdir(tmp_path):
    scratch = tempfile.mkdtemp(prefix="zenvi_url_import_", dir=str(tmp_path))
    file_path = os.path.join(scratch, "clip.mp4")
    open(file_path, "wb").write(b"x")
    assert os.path.isdir(scratch)
    cleanup_scratch_parent(file_path, "zenvi_url_import_")
    assert not os.path.isdir(scratch)


def test_cleanup_scratch_parent_ignores_unrelated_dirs(tmp_path):
    other = tmp_path / "keep_me"
    other.mkdir()
    file_path = other / "clip.mp4"
    file_path.write_bytes(b"x")
    cleanup_scratch_parent(str(file_path), "zenvi_url_import_")
    assert other.is_dir()


def test_saving_relocates_generated_media_that_is_still_open(tmp_path, monkeypatch):
    # #320: the timeline's reader holds a generated overlay open; on Windows a move
    # then fails with WinError 32 and the whole save failed
    from classes.assets import relocate_generated_media, reverse_media_moves

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    generated = tmp_path / "user" / "generated"
    generated.mkdir(parents=True)
    src = generated / "overlay.mov"
    src.write_bytes(b"overlay-bytes")
    project = tmp_path / "Founder.zvn"
    project.write_text("{}")
    files = [{"id": "F1", "path": str(src)}]
    clips = [{"file_id": "F1", "reader": {"path": str(src)}}]

    import shutil

    def locked(*_a, **_k):  # what Windows does while the reader holds the file open
        raise PermissionError(32, "The process cannot access the file")
    monkeypatch.setattr(shutil, "move", locked)
    moves = relocate_generated_media(files, clips, str(project))
    monkeypatch.undo()
    assert src.exists()  # copied, not moved

    dest = files[0]["path"]
    assert os.path.dirname(dest) == str(tmp_path / "Founder_assets" / "media")
    assert open(dest, "rb").read() == b"overlay-bytes"
    assert clips[0]["reader"]["path"] == dest

    reverse_media_moves(moves)  # a later save failure rolls back without losing the original
    assert src.read_bytes() == b"overlay-bytes"
    assert not os.path.exists(dest)


def test_a_failed_fallback_copy_leaves_no_partial_file(tmp_path, monkeypatch):
    import shutil

    from classes.assets import relocate_generated_media

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    generated = tmp_path / "user" / "generated"
    generated.mkdir(parents=True)
    src = generated / "overlay.mov"
    src.write_bytes(b"overlay-bytes")
    project = tmp_path / "Founder.zvn"
    project.write_text("{}")

    def locked(*_a, **_k):
        raise PermissionError(32, "in use")

    def disk_full(_src, dest, *a, **k):
        with open(dest, "wb") as fh:
            fh.write(b"over")  # half written
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(shutil, "move", locked)
    monkeypatch.setattr(shutil, "copy2", disk_full)
    with pytest.raises(OSError):
        relocate_generated_media([{"id": "F1", "path": str(src)}], [], str(project))
    media = tmp_path / "Founder_assets" / "media"
    assert not media.exists() or list(media.iterdir()) == []
