"""Media fingerprints for identity and one-folder relinking."""

import os
import shutil

from classes.media_fingerprint import (
    fingerprint,
    fingerprints_match,
    scan_folder_for_fingerprints,
)


def _write(path, body=b"media-body"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)


def test_fingerprint_stable_across_copy_and_rename(tmp_path):
    src = tmp_path / "a" / "clip.mp4"
    _write(str(src), b"hello-world-content" * 1000)
    fp1 = fingerprint(str(src))
    assert fp1 and fp1["sha256"] and fp1["size"] > 0

    copied = tmp_path / "b" / "renamed.mp4"
    copied.parent.mkdir()
    shutil.copy2(str(src), str(copied))
    fp2 = fingerprint(str(copied))
    assert fingerprints_match(fp1, fp2)


def test_same_size_different_content_do_not_match(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    body_a = b"A" * 2048
    body_b = b"B" * 2048
    a.write_bytes(body_a)
    b.write_bytes(body_b)
    assert not fingerprints_match(fingerprint(str(a)), fingerprint(str(b)))


def test_sampled_fingerprint_can_collide_full_hash_does_not(tmp_path):
    from classes.media_fingerprint import files_identical

    head = b"H" * (1024 * 1024)
    tail = b"T" * (1024 * 1024)
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(head + (b"A" * 4096) + tail)
    b.write_bytes(head + (b"B" * 4096) + tail)
    assert fingerprints_match(fingerprint(str(a)), fingerprint(str(b)))
    assert not files_identical(str(a), str(b))


def test_small_file_fingerprint(tmp_path):
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"x")
    fp = fingerprint(str(tiny))
    assert fp and fp["size"] == 1 and fp["sha256"]


def test_zero_byte_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    fp = fingerprint(str(empty))
    assert fp and fp["size"] == 0 and fp["sha256"]


def test_missing_and_sequence_return_none(tmp_path):
    assert fingerprint(str(tmp_path / "missing.mp4")) is None
    assert fingerprint(str(tmp_path / "frame%04d.png")) is None


def test_scan_folder_finds_wanted_digests(tmp_path):
    folder = tmp_path / "media"
    target = folder / "sub" / "clip.mp4"
    other = folder / "noise.mp4"
    _write(str(target), b"target-bytes" * 500)
    _write(str(other), b"noise-bytes" * 500)
    wanted = fingerprint(str(target))["sha256"]
    found = scan_folder_for_fingerprints(str(folder), wanted={wanted})
    assert found[wanted] == str(target)


def test_folder_relink_updates_files_and_clips(monkeypatch, tmp_path):
    """check_if_paths_are_valid fingerprint-matches a whole folder in one pass."""
    import pytest
    pytest.importorskip("openshot")
    pytest.importorskip("PyQt5.QtWidgets")

    from classes import project_data as pd
    from classes.media_fingerprint import fingerprint as fp_fn

    original = tmp_path / "original" / "clip.mp4"
    _write(str(original), b"unique-clip-bytes" * 800)
    fp = fp_fn(str(original))
    moved_dir = tmp_path / "relocated"
    moved = moved_dir / "clip.mp4"
    moved_dir.mkdir()
    shutil.copy2(str(original), str(moved))
    os.remove(str(original))

    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store.current_filepath = str(tmp_path / "proj.zvn")
    store._data = {
        "files": [{"id": "f1", "path": str(original), "fingerprint": fp}],
        "clips": [{"id": "c1", "file_id": "f1", "reader": {"path": str(original)}}],
    }

    class _Settings:
        class actionType:
            IMPORT = 1

        def setDefaultPath(self, *a):
            pass

        def get(self, key):
            return []

        def set(self, key, value):
            pass

        def save(self):
            pass

    class _App:
        window = None

        def _tr(self, s):
            return s

        def get_settings(self):
            return _Settings()

    monkeypatch.setattr(pd, "get_app", lambda: _App())

    class _Btn:
        pass

    class _Msg:
        AcceptRole = 0
        ActionRole = 1

        def __init__(self, *a, **k):
            self.locate = _Btn()

        def setWindowTitle(self, *a):
            pass

        def setText(self, *a):
            pass

        def setInformativeText(self, *a):
            pass

        def addButton(self, text, role):
            return self.locate if "Locate" in str(text) else _Btn()

        def exec_(self):
            return 0

        def clickedButton(self):
            return self.locate

    monkeypatch.setattr(pd, "QMessageBox", _Msg)

    class _FileDialog:
        @staticmethod
        def getExistingDirectory(*a, **k):
            return str(moved_dir)

    monkeypatch.setattr(pd, "QFileDialog", _FileDialog)

    store.check_if_paths_are_valid()
    assert store._data["files"][0]["path"] == str(moved)
    assert store._data["clips"][0]["reader"]["path"] == str(moved)


def test_locate_cancel_keeps_files_and_clips(monkeypatch, tmp_path):
    import pytest
    pytest.importorskip("openshot")
    pytest.importorskip("PyQt5.QtWidgets")

    from classes import project_data as pd

    missing = str(tmp_path / "gone.mp4")
    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store.current_filepath = None
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
            self.locate = _Btn()

        def setWindowTitle(self, *a):
            pass

        def setText(self, *a):
            pass

        def setInformativeText(self, *a):
            pass

        def addButton(self, text, role):
            return self.locate if "Locate" in str(text) else _Btn()

        def exec_(self):
            return 0

        def clickedButton(self):
            return self.locate

    monkeypatch.setattr(pd, "QMessageBox", _Msg)

    class _FileDialog:
        @staticmethod
        def getExistingDirectory(*a, **k):
            return ""

    monkeypatch.setattr(pd, "QFileDialog", _FileDialog)

    store.check_if_paths_are_valid()
    assert len(store._data["files"]) == 1
    assert len(store._data["clips"]) == 1
