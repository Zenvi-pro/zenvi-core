"""Dragging a Project Files item onto the web timeline must place it once.

Regression for the duplicate-clip drop: ``files_model.mimeData`` attaches the
dragged files both as JSON ids (text/html "clip" + text/plain) and as URLs.
The timeline's drag-enter handler used to take both branches, re-import the
URLs, get the same ids back, and create two clips per file.
"""

from types import SimpleNamespace

from classes.file_drop import os_drop_file_ids


class _Mime:
    """Just enough of QMimeData for the file_drop helpers."""

    def __init__(self, html="", text="", urls=()):
        self._html = html
        self._text = text
        self._urls = list(urls)

    def html(self):
        return self._html

    def text(self):
        return self._text

    def hasText(self):
        return bool(self._text)

    def hasUrls(self):
        return bool(self._urls)

    def urls(self):
        return list(self._urls)

    # No text/uri-list here on purpose: under the headless stub QUrl is a
    # MagicMock, and file_drop's macOS ``/.file/id=`` resolver would hand it
    # to ctypes. ``urls()`` is the branch the timeline relies on.
    def formats(self):
        return []

    def hasFormat(self, fmt):
        return False

    def data(self, fmt):
        return b""


class _Url:
    def __init__(self, path):
        self._path = path

    def isLocalFile(self):
        return True

    def toLocalFile(self):
        return self._path

    def toString(self):
        return "file://" + self._path

    def scheme(self):
        return "file"

    def path(self):
        return self._path


def _project_files_drag(tmp_path):
    """Mime as built by files_model.mimeData for one selected file."""
    clip = tmp_path / "videoplayback.mp4"
    clip.write_bytes(b"x")
    return _Mime(html="clip", text='["FILE1"]', urls=[_Url(str(clip))]), clip


def test_project_files_drag_does_not_reimport_or_duplicate(tmp_path):
    mime, _ = _project_files_drag(tmp_path)
    json_ids = ["FILE1"]  # what _mime_json_list parsed from text/plain
    calls = []

    def import_urls(urls):
        calls.append(urls)
        return [SimpleNamespace(id="FILE1")]

    extra = os_drop_file_ids(mime, json_ids, import_urls)

    assert extra == []
    assert calls == [], "an in-app drag must not be re-imported as an OS drop"
    assert json_ids + extra == ["FILE1"]


def test_os_file_drop_still_imports_and_places(tmp_path):
    clip = tmp_path / "dropped.mov"
    clip.write_bytes(b"x")
    mime = _Mime(urls=[_Url(str(clip))])  # Finder drop: URLs, no html/text
    seen = []

    def import_urls(urls):
        seen.append([u.toLocalFile() for u in urls])
        return [SimpleNamespace(id="NEW1"), None, SimpleNamespace(id=None)]

    assert os_drop_file_ids(mime, [], import_urls) == ["NEW1"]
    assert seen == [[str(clip)]]


def test_html_drag_with_no_ids_falls_back_to_urls(tmp_path):
    # Defensive: html present but the text payload was empty/unparseable.
    mime, clip = _project_files_drag(tmp_path)
    mime._text = ""

    def import_urls(urls):
        assert [u.toLocalFile() for u in urls] == [str(clip)]
        return [SimpleNamespace(id="FILE1")]

    assert os_drop_file_ids(mime, [], import_urls) == ["FILE1"]


def test_no_urls_and_no_ids_imports_nothing():
    calls = []
    assert os_drop_file_ids(_Mime(html="clip"), [], calls.append) == []
    assert calls == []
