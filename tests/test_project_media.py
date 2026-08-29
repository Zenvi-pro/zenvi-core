"""Imported media is copied into the project assets folder; skip-all keeps clips."""

import os

from classes.assets import copy_imported_media, path_is_under


def _write(path, body=b"media"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)


def test_path_is_under_rejects_sibling_prefix(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert path_is_under(str(root / "media" / "a.mp4"), str(root))
    assert not path_is_under(str(tmp_path / "proj_other" / "a.mp4"), str(root))


def test_copy_imported_media_moves_file_and_clip_paths(tmp_path):
    src = tmp_path / "Downloads" / "clip.mp4"
    _write(str(src))
    project = str(tmp_path / "MyProject.zvn")
    files = [{"id": "f1", "path": str(src)}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": str(src)}}]

    copy_imported_media(files, clips, project)

    dest = files[0]["path"]
    assert dest != str(src)
    assert dest.endswith(os.path.join("MyProject_assets", "media", "clip.mp4"))
    assert os.path.isfile(dest)
    assert os.path.isfile(str(src))  # original stays
    assert clips[0]["reader"]["path"] == dest


def test_copy_imported_media_skips_files_already_in_assets(tmp_path):
    project = str(tmp_path / "MyProject.zvn")
    assets_media = tmp_path / "MyProject_assets" / "media"
    assets_media.mkdir(parents=True)
    already = assets_media / "kept.mp4"
    _write(str(already), b"inside")
    files = [{"id": "f1", "path": str(already)}]

    copy_imported_media(files, [], project)

    assert files[0]["path"] == str(already)


def test_copy_imported_media_skips_bundled_app_resources(tmp_path):
    app_root = tmp_path / "app"
    bundled = app_root / "transitions" / "fade.svg"
    _write(str(bundled), b"svg")
    project = str(tmp_path / "MyProject.zvn")
    files = [{"id": "f1", "path": str(bundled)}]

    copy_imported_media(files, [], project, app_root=str(app_root))

    assert files[0]["path"] == str(bundled)
    media_dir = tmp_path / "MyProject_assets" / "media"
    assert not media_dir.exists() or not os.listdir(media_dir)


def test_copy_imported_media_disambiguates_basename_collisions(tmp_path):
    a = tmp_path / "a" / "same.mp4"
    b = tmp_path / "b" / "same.mp4"
    _write(str(a), b"one")
    _write(str(b), b"two")
    project = str(tmp_path / "MyProject.zvn")
    files = [
        {"id": "f1", "path": str(a)},
        {"id": "f2", "path": str(b)},
    ]

    copy_imported_media(files, [], project)

    names = {os.path.basename(f["path"]) for f in files}
    assert "same.mp4" in names
    assert "same_f2.mp4" in names
    assert os.path.isfile(files[0]["path"])
    assert os.path.isfile(files[1]["path"])
    assert files[0]["path"] != files[1]["path"]


def test_copy_imported_media_skips_missing_and_sequences(tmp_path):
    project = str(tmp_path / "MyProject.zvn")
    files = [
        {"id": "gone", "path": str(tmp_path / "nope.mp4")},
        {"id": "seq", "path": str(tmp_path / "frame%04d.png")},
    ]
    copy_imported_media(files, [], project)
    assert files[0]["path"].endswith("nope.mp4")
    assert "%04d" in files[1]["path"]


def test_copy_imported_media_updates_clip_path_without_file_id(tmp_path):
    src = tmp_path / "stock.mp4"
    _write(str(src))
    project = str(tmp_path / "P.zvn")
    files = [{"id": "f1", "path": str(src)}]
    clips = [{"id": "c1", "reader": {"path": str(src)}}]

    copy_imported_media(files, clips, project)

    assert clips[0]["reader"]["path"] == files[0]["path"]


def test_skip_all_keeps_missing_files_and_clips(monkeypatch, tmp_path):
    import pytest
    pytest.importorskip("openshot")
    pytest.importorskip("PyQt5.QtWidgets")

    from classes import project_data as pd

    missing = str(tmp_path / "gone.mp4")
    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store._data = {
        "files": [{"id": "f1", "path": missing}],
        "clips": [{"id": "c1", "file_id": "f1", "reader": {"path": missing}}],
    }

    class _App:
        window = None

        def _tr(self, s):
            return s

        def get_settings(self):
            return None

    monkeypatch.setattr(pd, "get_app", lambda: _App())

    class _Btn:
        pass

    class _Msg:
        AcceptRole = 0
        ActionRole = 1

        def __init__(self, *a, **k):
            self.skip = _Btn()

        def setWindowTitle(self, *a):
            pass

        def setText(self, *a):
            pass

        def setInformativeText(self, *a):
            pass

        def addButton(self, text, role):
            return self.skip if "Skip" in str(text) else _Btn()

        def exec_(self):
            return 0

        def clickedButton(self):
            return self.skip

    monkeypatch.setattr(pd, "QMessageBox", _Msg)
    store.check_if_paths_are_valid()
    assert len(store._data["files"]) == 1
    assert len(store._data["clips"]) == 1
    assert store._data["clips"][0]["id"] == "c1"
