"""Generated media relocates on save; imported footage is referenced in place."""

import os

from classes import info
from classes.assets import (
    path_is_under,
    relocate_generated_media,
    restore_media_paths,
    reverse_media_moves,
    snapshot_media_paths,
)


def _write(path, body=b"media"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)


def test_path_is_under_rejects_sibling_prefix(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert path_is_under(str(root / "media" / "a.mp4"), str(root))
    assert not path_is_under(str(tmp_path / "proj_other" / "a.mp4"), str(root))


def test_relocate_leaves_external_imports_in_place(tmp_path):
    src = tmp_path / "Downloads" / "clip.mp4"
    _write(str(src))
    project = str(tmp_path / "MyProject.zvn")
    files = [{"id": "f1", "path": str(src)}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": str(src)}}]

    moves = relocate_generated_media(files, clips, project)

    assert moves == []
    assert files[0]["path"] == str(src)
    assert os.path.isfile(str(src))
    media_dir = tmp_path / "MyProject_assets" / "media"
    assert not media_dir.exists() or not list(media_dir.iterdir())


def test_relocate_moves_generated_from_user_path(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    gen_dir = tmp_path / "user" / "generated"
    gen_dir.mkdir(parents=True)
    src = gen_dir / "generated_abc.mp4"
    _write(str(src), b"gen")
    unrelated = gen_dir / "other_project.mp4"
    _write(str(unrelated), b"keep")

    project = str(tmp_path / "MyProject.zvn")
    files = [{"id": "f1", "path": str(src)}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": str(src)}}]

    moves = relocate_generated_media(files, clips, project)

    assert len(moves) == 1
    dest = files[0]["path"]
    assert dest.endswith(os.path.join("MyProject_assets", "media", "generated_abc.mp4"))
    assert os.path.isfile(dest)
    assert not os.path.exists(str(src))
    assert os.path.isfile(str(unrelated))
    assert clips[0]["reader"]["path"] == dest


def test_relocate_skips_files_already_in_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    project = str(tmp_path / "MyProject.zvn")
    assets_media = tmp_path / "MyProject_assets" / "media"
    assets_media.mkdir(parents=True)
    already = assets_media / "kept.mp4"
    _write(str(already), b"inside")
    files = [{"id": "f1", "path": str(already)}]

    moves = relocate_generated_media(files, [], project)

    assert moves == []
    assert files[0]["path"] == str(already)


def test_relocate_disambiguates_until_unused(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    gen_dir = tmp_path / "user" / "generated"
    gen_dir.mkdir(parents=True)
    a = gen_dir / "same.mp4"
    _write(str(a), b"one")
    project = str(tmp_path / "MyProject.zvn")
    assets_media = tmp_path / "MyProject_assets" / "media"
    assets_media.mkdir(parents=True)
    _write(str(assets_media / "same.mp4"), b"existing")
    _write(str(assets_media / "same_f2.mp4"), b"also-taken")

    files = [{"id": "f2", "path": str(a)}]
    moves = relocate_generated_media(files, [], project)

    assert len(moves) == 1
    assert os.path.basename(files[0]["path"]) == "same_f2_1.mp4"
    assert os.path.isfile(str(assets_media / "same.mp4"))
    assert os.path.isfile(str(assets_media / "same_f2.mp4"))
    assert os.path.isfile(files[0]["path"])


def test_relocate_restores_paths_when_later_move_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    gen_dir = tmp_path / "user" / "generated"
    gen_dir.mkdir(parents=True)
    first = gen_dir / "first.mp4"
    second = gen_dir / "second.mp4"
    _write(str(first), b"one")
    _write(str(second), b"two")
    project = str(tmp_path / "MyProject.zvn")
    files = [
        {"id": "f1", "path": str(first)},
        {"id": "f2", "path": str(second)},
    ]
    clips = [
        {"id": "c1", "file_id": "f1", "reader": {"path": str(first)}},
        {"id": "c2", "file_id": "f2", "reader": {"path": str(second)}},
    ]

    real_move = __import__("shutil").move
    call_count = {"n": 0}

    def _flaky_move(src, dest):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("disk full")
        return real_move(src, dest)

    monkeypatch.setattr("classes.assets.shutil.move", _flaky_move)

    import pytest

    with pytest.raises(OSError, match="disk full"):
        relocate_generated_media(files, clips, project)

    assert files[0]["path"] == str(first)
    assert files[1]["path"] == str(second)
    assert clips[0]["reader"]["path"] == str(first)
    assert clips[1]["reader"]["path"] == str(second)
    assert os.path.isfile(str(first))
    assert os.path.isfile(str(second))


def test_relocate_skips_missing_and_sequences(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    project = str(tmp_path / "MyProject.zvn")
    files = [
        {"id": "gone", "path": str(tmp_path / "user" / "generated" / "nope.mp4")},
        {"id": "seq", "path": str(tmp_path / "user" / "generated" / "frame%04d.png")},
    ]
    moves = relocate_generated_media(files, [], project)
    assert moves == []
    assert files[0]["path"].endswith("nope.mp4")
    assert "%04d" in files[1]["path"]


def test_relocate_updates_clip_path_without_file_id(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    gen_dir = tmp_path / "user" / "generated"
    gen_dir.mkdir(parents=True)
    src = gen_dir / "stock.mp4"
    _write(str(src))
    project = str(tmp_path / "P.zvn")
    files = [{"id": "f1", "path": str(src)}]
    clips = [{"id": "c1", "reader": {"path": str(src)}}]

    relocate_generated_media(files, clips, project)

    assert clips[0]["reader"]["path"] == files[0]["path"]


def test_reverse_media_moves_restores_files(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    gen_dir = tmp_path / "user" / "generated"
    gen_dir.mkdir(parents=True)
    src = gen_dir / "generated_x.mp4"
    _write(str(src), b"body")
    project = str(tmp_path / "MyProject.zvn")
    files = [{"id": "f1", "path": str(src)}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": str(src)}}]

    snapshot = snapshot_media_paths(files, clips)
    moves = relocate_generated_media(files, clips, project)
    assert not os.path.exists(str(src))
    assert os.path.isfile(files[0]["path"])

    reverse_media_moves(moves)
    restore_media_paths(snapshot)
    assert files[0]["path"] == str(src)
    assert clips[0]["reader"]["path"] == str(src)
    assert os.path.isfile(str(src))


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


def test_save_restores_moves_if_write_fails(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("openshot")
    pytest.importorskip("PyQt5.QtWidgets")

    from classes import project_data as pd

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    gen_dir = tmp_path / "user" / "generated"
    gen_dir.mkdir(parents=True)
    src = gen_dir / "generated_y.mp4"
    _write(str(src), b"body")
    project = str(tmp_path / "MyProject.zvn")
    files = [{"id": "f1", "path": str(src)}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": str(src)}}]

    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store._data = {"files": files, "clips": clips, "version": {}}
    store.current_filepath = None
    monkeypatch.setattr(store, "move_temp_paths_to_project_folder", lambda *a, **k: None)

    # Stub openshot version attribute used during save.
    import openshot
    monkeypatch.setattr(openshot, "OPENSHOT_VERSION_FULL", "0", raising=False)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_to_file", _boom)

    with pytest.raises(OSError, match="disk full"):
        store.save(project)

    assert files[0]["path"] == str(src)
    assert clips[0]["reader"]["path"] == str(src)
    assert os.path.isfile(str(src))
