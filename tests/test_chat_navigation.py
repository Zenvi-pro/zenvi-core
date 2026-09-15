"""Keep the Agents webview on chat_ui/ when a video is dropped."""

from classes.chat_navigation import is_allowed_chat_navigation



class _FakeUrl:
    def __init__(self, scheme, path="", local=True):
        self._scheme = scheme
        self._path = path
        self._local = local

    def scheme(self):
        return self._scheme

    def isLocalFile(self):
        return self._local

    def toLocalFile(self):
        return self._path


def test_allows_chat_ui_assets(tmp_path):
    chat_dir = tmp_path / "chat_ui"
    chat_dir.mkdir()
    index = chat_dir / "index.html"
    index.write_text("<html></html>")
    url = _FakeUrl("file", str(index), local=True)
    assert is_allowed_chat_navigation(url, str(chat_dir)) is True
    css = chat_dir / "chat.css"
    css.write_text("body{}")
    assert is_allowed_chat_navigation(_FakeUrl("file", str(css), local=True), str(chat_dir))


def test_rejects_dropped_video(tmp_path):
    chat_dir = tmp_path / "chat_ui"
    chat_dir.mkdir()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    url = _FakeUrl("file", str(clip), local=True)
    assert is_allowed_chat_navigation(url, str(chat_dir)) is False


def test_allows_data_and_qrc_schemes(tmp_path):
    chat_dir = str(tmp_path / "chat_ui")
    assert is_allowed_chat_navigation(_FakeUrl("data"), chat_dir)
    assert is_allowed_chat_navigation(_FakeUrl("qrc"), chat_dir)
    assert is_allowed_chat_navigation(_FakeUrl("about"), chat_dir)


def test_rejects_http(tmp_path):
    chat_dir = str(tmp_path / "chat_ui")
    url = _FakeUrl("https", "https://example.com/video.mp4", local=False)
    assert is_allowed_chat_navigation(url, chat_dir) is False
    assert is_allowed_chat_navigation(None, chat_dir) is False
