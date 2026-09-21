"""Path expansion / glob / folder walk for OS drops and agent import."""

import os

import pytest

from classes.file_drop import (
    collect_import_paths,
    flatten_path_args,
    local_path_from_url,
    media_add_dirs,
    msys_path_to_windows,
    normalize_agent_fs_path,
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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/c/Users/alice/Desktop/clips", r"C:\Users\alice\Desktop\clips"),
        ("/C/Users/bob", r"C:\Users\bob"),
        ("/cygdrive/d/media/take.mp4", r"D:\media\take.mp4"),
        ("/cygdrive/C/", "C:\\"),
        (r"C:\Users\alice", None),
        ("~/Desktop", None),
        ("", None),
        # /Users/... is NOT an MSYS drive path (/c/Users is).
        ("/Users/alice/Desktop/clips", None),
    ],
)
def test_msys_path_to_windows(raw, expected):
    assert msys_path_to_windows(raw) == expected


def test_msys_windows_path_if_usable_requires_existing_drive(monkeypatch):
    from classes.file_drop import _msys_windows_path_if_usable
    import classes.file_drop as fd

    monkeypatch.setattr(fd, "_running_on_windows", lambda: False)
    assert _msys_windows_path_if_usable("/c/Users/alice") is None

    monkeypatch.setattr(fd, "_running_on_windows", lambda: True)
    monkeypatch.setattr(
        fd, "_windows_drive_root_exists",
        lambda drive: str(drive).upper().rstrip(":\\") == "C",
    )
    assert _msys_windows_path_if_usable("/c/Users/alice/Videos") == r"C:\Users\alice\Videos"
    # Would map to U:\, which our stub says is missing.
    assert _msys_windows_path_if_usable("/u/Users/alice/Desktop") is None


def test_normalize_agent_fs_path_uses_msys_gate(monkeypatch):
    import classes.file_drop as fd

    seen = []

    def fake_resolve(path, home=None):
        seen.append(path)
        return path

    monkeypatch.setattr(fd, "_running_on_windows", lambda: True)
    monkeypatch.setattr(fd, "resolve_user_path", fake_resolve)
    monkeypatch.setattr(
        fd,
        "_msys_windows_path_if_usable",
        lambda path: msys_path_to_windows(path)
        if str(path).lower().startswith(("/c/", "/cygdrive/"))
        else None,
    )

    assert normalize_agent_fs_path("/c/Users/alice/Videos") == r"C:\Users\alice\Videos"
    assert seen[-1] == r"C:\Users\alice\Videos"

    seen.clear()
    assert normalize_agent_fs_path("file:///c/Users/alice/clip.mp4") == r"C:\Users\alice\clip.mp4"
    assert seen[-1] == r"C:\Users\alice\clip.mp4"

    seen.clear()
    assert normalize_agent_fs_path("/Users/alice/Desktop/clips") == "/Users/alice/Desktop/clips"
    assert seen == ["/Users/alice/Desktop/clips"]


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


def test_names_are_adjacent_variants():
    from classes.file_drop import names_are_adjacent

    assert names_are_adjacent("dirty_test", "dirty_test")
    assert names_are_adjacent("dirty_test", "dirty-test")
    assert names_are_adjacent("Dirty Test", "dirty_test")
    assert names_are_adjacent("dirty_tes", "dirty_test")
    assert not names_are_adjacent("wedding", "dirty_test")
    assert not names_are_adjacent("ab", "abc")  # too short for prefix rule


def test_resolve_agent_import_target_exact(tmp_path):
    from classes.file_drop import resolve_agent_import_target

    folder = tmp_path / "dirty_test"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"x")
    result = resolve_agent_import_target(str(folder), home=str(tmp_path))
    assert result["status"] == "ok"
    assert result["match"] == "exact"
    assert result["path"] == str(folder)


def test_resolve_agent_import_target_adjacent_one_hit(tmp_path):
    from classes.file_drop import resolve_agent_import_target

    real = tmp_path / "dirty_test"
    real.mkdir()
    (real / "clip.mp4").write_bytes(b"x")
    wrong = tmp_path / "dirty_tes"
    result = resolve_agent_import_target(str(wrong), home=str(tmp_path))
    assert result["status"] == "ok"
    assert result["match"] == "adjacent"
    assert result["path"] == str(real)
    assert result["from"] == str(wrong)


def test_resolve_agent_import_target_adjacent_under_downloads(tmp_path, monkeypatch):
    from classes.file_drop import resolve_agent_import_target

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    real = downloads / "dirty-test"
    real.mkdir()
    (real / "a.mp4").write_bytes(b"x")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    result = resolve_agent_import_target("dirty_test", home=str(tmp_path))
    assert result["status"] == "ok"
    assert result["match"] in ("exact", "adjacent")
    assert result["path"] == str(real)


def test_resolve_agent_import_target_ambiguous(tmp_path):
    from classes.file_drop import resolve_agent_import_target

    a = tmp_path / "clip_final"
    b = tmp_path / "clip_rough"
    a.mkdir()
    b.mkdir()
    # Both are adjacent to "clip" via the prefix rule.
    result = resolve_agent_import_target(str(tmp_path / "clip"), home=str(tmp_path))
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) >= 2


def test_resolve_agent_import_target_missing(tmp_path):
    from classes.file_drop import resolve_agent_import_target

    result = resolve_agent_import_target(
        str(tmp_path / "no_such_folder_xyz"), home=str(tmp_path)
    )
    assert result["status"] == "missing"


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
