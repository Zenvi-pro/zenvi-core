"""Generated media is born durable — never under the OS temp directory."""

import os
import tempfile

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
