"""Path expansion / glob / folder walk for OS drops and agent import."""

import os

import pytest

from classes.file_drop import (
    collect_import_paths,
    flatten_path_args,
    local_path_from_url,
    media_add_dirs,
    resolve_user_path,
)


def test_flatten_path_args_splits_comma_newline_and_lists():
    assert flatten_path_args("a.mp4, b.mov\nc.wav", ["d.png", ""]) == [
        "a.mp4", "b.mov", "c.wav", "d.png",
    ]
    assert flatten_path_args(None, "", False) == []


def test_flatten_keeps_existing_path_that_contains_a_comma(tmp_path):
    weird = tmp_path / "take, 2.mp4"
    weird.write_bytes(b"x")
    assert flatten_path_args(str(weird)) == [str(weird)]


def test_collect_import_paths_walks_directory_and_skips_missing(tmp_path):
    footage = tmp_path / "footage"
    nested = footage / "day1"
    nested.mkdir(parents=True)
    (nested / "clip.mp4").write_bytes(b"x")
    (footage / "take.mov").write_bytes(b"y")

    files, notes = collect_import_paths([str(footage), str(tmp_path / "gone.mp4")])
    assert notes == [f"Not found: {tmp_path / 'gone.mp4'}"]
    assert [os.path.basename(p) for p in files] == ["clip.mp4", "take.mov"]


def test_collect_import_paths_dedupes_file_and_parent_dir(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    files, notes = collect_import_paths([str(clip), str(tmp_path)])
    assert not notes
    assert files == [str(clip)]


def test_collect_import_paths_expands_glob(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"a")
    (tmp_path / "b.mp4").write_bytes(b"b")
    (tmp_path / "skip.txt").write_bytes(b"s")
    files, notes = collect_import_paths(str(tmp_path / "*.mp4"))
    assert not notes
    assert [os.path.basename(p) for p in files] == ["a.mp4", "b.mp4"]


def test_resolve_user_path_tilde_and_media_folder(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    clip = desktop / "reel.mp4"
    clip.write_bytes(b"z")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    def _expand(path):
        if path == "~" or path.startswith("~/"):
            return str(tmp_path) + path[1:]
        return path

    monkeypatch.setattr(os.path, "expanduser", _expand)

    assert resolve_user_path("~/Desktop/reel.mp4", home=str(tmp_path)) == str(clip)
    assert resolve_user_path("reel.mp4", home=str(tmp_path)) == str(clip)
    assert resolve_user_path("Desktop/reel.mp4", home=str(tmp_path)) == str(clip)


def test_local_path_from_url_string_and_none(tmp_path):
    clip = tmp_path / "n.mp4"
    clip.write_bytes(b"n")
    assert local_path_from_url(None) == ""
    assert local_path_from_url(str(clip)) == str(clip)
    file_url = "file://" + str(clip)
    assert os.path.normpath(local_path_from_url(file_url)) == os.path.normpath(str(clip))
    files, notes = collect_import_paths(file_url)
    assert not notes
    assert files == [str(clip)]
    bogus = local_path_from_url("file:///.file/id=1.2")
    assert bogus == ""


def test_media_add_dirs_only_existing_folders(tmp_path):
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Downloads").mkdir()
    dirs = media_add_dirs(str(tmp_path))
    names = {os.path.basename(p) for p in dirs}
    assert names == {"Desktop", "Downloads"}
    assert all(os.path.isdir(p) for p in dirs)


def test_mime_has_file_drop_and_urls_from_mime(tmp_path):
    pytest.importorskip("PyQt5.QtCore")
    from PyQt5.QtCore import QMimeData, QUrl
    from classes.file_drop import mime_has_file_drop, urls_from_mime

    empty = QMimeData()
    assert mime_has_file_drop(empty) is False
    assert urls_from_mime(empty) == []

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    other = tmp_path / "other.mov"
    other.write_bytes(b"y")

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(clip))])
    assert mime_has_file_drop(mime) is True
    urls = urls_from_mime(mime)
    assert len(urls) == 1
    assert os.path.normpath(urls[0].toLocalFile()) == os.path.normpath(str(clip))

    listed = QMimeData()
    listed.setData("text/uri-list", QUrl.fromLocalFile(str(other)).toString().encode("utf-8") + b"\n")
    assert mime_has_file_drop(listed) is True
    listed_urls = urls_from_mime(listed)
    assert listed_urls
    assert os.path.normpath(listed_urls[0].toLocalFile()) == os.path.normpath(str(other))


def test_mime_has_file_drop_promised_file_format():
    pytest.importorskip("PyQt5.QtCore")
    from PyQt5.QtCore import QMimeData
    from classes.file_drop import mime_has_file_drop

    mime = QMimeData()
    mime.setData("com.apple.pasteboard.promised-file-url", b"")
    assert mime_has_file_drop(mime) is True


def test_urls_from_mime_prefers_existing_text_over_file_id(tmp_path):
    pytest.importorskip("PyQt5.QtCore")
    from PyQt5.QtCore import QMimeData, QUrl
    from classes.file_drop import urls_from_mime

    clip = tmp_path / "real.mp4"
    clip.write_bytes(b"x")
    mime = QMimeData()
    mime.setUrls([QUrl("file:///.file/id=1.2")])
    mime.setText(str(clip))
    urls = urls_from_mime(mime)
    assert urls
    assert os.path.normpath(urls[0].toLocalFile()) == os.path.normpath(str(clip))


def test_clip_library_drag_is_not_an_os_file_drop():
    pytest.importorskip("PyQt5.QtCore")
    from PyQt5.QtCore import QMimeData
    from classes.file_drop import mime_has_file_drop

    mime = QMimeData()
    mime.setHtml("clip")
    mime.setText('["abc123"]')
    assert mime_has_file_drop(mime) is False



def test_local_path_from_url_rejects_remote_http_urls():
    """http(s) QUrls must not resolve to a local path such as /etc/passwd."""
    pytest.importorskip("PyQt5.QtCore")
    from PyQt5.QtCore import QUrl

    https_url = QUrl("https://example.invalid/etc/passwd")
    assert local_path_from_url(https_url) == ""
    assert local_path_from_url(https_url) != os.path.abspath("/etc/passwd")

    http_url = QUrl("http://127.0.0.1/tmp/x")
    assert local_path_from_url(http_url) == ""

    files, _notes = collect_import_paths([
        https_url.toString(),
        http_url.toString(),
    ])
    assert files == []
    local_hits = {
        os.path.abspath("/etc/passwd"),
        os.path.abspath("/tmp/x"),
        "/etc/passwd",
        "/tmp/x",
    }
    assert not local_hits.intersection(files)
