"""The missing-files prompt counts and names each missing file once.

Testing RC v1.2.0 (#216): after moving two media files a project used, opening
it said "This project references 4 missing file(s)" and listed each name twice.
A file is referenced by its Project Files entry *and* by every clip cut from
it, and each reference was counted as another missing file.
"""

from classes import path_utils
from classes import project_data as pd


def _open_with_missing_media(monkeypatch, tmp_path, files, clips):
    """Run the missing-files check and return the prompts it showed (skip all)."""
    shown = []

    class _Prompt:
        AcceptRole = 0
        ActionRole = 1

        def __init__(self, *a, **k):
            self.text = ""
            self.informative = ""
            self._skip = object()
            shown.append(self)

        def setWindowTitle(self, *a):
            pass

        def setText(self, text):
            self.text = text

        def setInformativeText(self, text):
            self.informative = text

        def addButton(self, text, role):
            return self._skip if "Skip" in str(text) else object()

        def exec_(self):
            return 0

        def clickedButton(self):
            return self._skip

    class _App:
        window = None

        def _tr(self, s):
            return s

        def get_settings(self):
            return None

    monkeypatch.setattr(pd, "get_app", lambda: _App())
    monkeypatch.setattr(pd, "QMessageBox", _Prompt)
    # No remembered media roots: nothing may be relinked silently.
    monkeypatch.setattr(path_utils, "_media_roots", lambda: [])

    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store.current_filepath = str(tmp_path / "project.zvn")
    store._data = {"files": files, "clips": clips}
    store.check_if_paths_are_valid()
    return shown


def test_file_and_its_clip_count_as_one_missing_file(monkeypatch, tmp_path):
    """Two moved files, each on the timeline: the RC repro."""
    a = str(tmp_path / "gone" / "a_roll.mp4")
    b = str(tmp_path / "gone" / "b_roll.mp4")
    files = [{"id": "f1", "path": a}, {"id": "f2", "path": b}]
    clips = [
        {"id": "c1", "file_id": "f1", "reader": {"path": a}},
        {"id": "c2", "file_id": "f2", "reader": {"path": b}},
    ]

    shown = _open_with_missing_media(monkeypatch, tmp_path, files, clips)

    assert len(shown) == 1
    assert shown[0].text == "This project references 2 missing file(s)."
    assert shown[0].informative == "a_roll.mp4, b_roll.mp4"


def test_several_clips_cut_from_one_file_count_once(monkeypatch, tmp_path):
    a = str(tmp_path / "gone" / "interview.mp4")
    files = [{"id": "f1", "path": a}]
    clips = [
        {"id": "c%d" % i, "file_id": "f1", "reader": {"path": a}} for i in range(3)
    ]

    shown = _open_with_missing_media(monkeypatch, tmp_path, files, clips)

    assert shown[0].text == "This project references 1 missing file(s)."
    assert shown[0].informative == "interview.mp4"


def test_clip_with_its_own_missing_path_still_counts(monkeypatch, tmp_path):
    """A clip whose reader points somewhere no Project Files entry does is its
    own missing file; de-duplicating must not hide it."""
    a = str(tmp_path / "gone" / "a_roll.mp4")
    orphan = str(tmp_path / "elsewhere" / "orphan.mp4")
    files = [{"id": "f1", "path": a}]
    clips = [
        {"id": "c1", "file_id": "f1", "reader": {"path": a}},
        {"id": "c2", "file_id": "f9", "reader": {"path": orphan}},
    ]

    shown = _open_with_missing_media(monkeypatch, tmp_path, files, clips)

    assert shown[0].text == "This project references 2 missing file(s)."
    assert shown[0].informative == "a_roll.mp4, orphan.mp4"


def test_more_than_five_unique_files_summarises_the_rest(monkeypatch, tmp_path):
    names = ["clip_%d.mp4" % i for i in range(7)]
    paths = [str(tmp_path / "gone" / n) for n in names]
    files = [{"id": "f%d" % i, "path": p} for i, p in enumerate(paths)]
    clips = [
        {"id": "c%d" % i, "file_id": "f%d" % i, "reader": {"path": p}}
        for i, p in enumerate(paths)
    ]

    shown = _open_with_missing_media(monkeypatch, tmp_path, files, clips)

    assert shown[0].text == "This project references 7 missing file(s)."
    assert shown[0].informative == "%s and 2 more" % ", ".join(names[:5])


def test_nothing_missing_shows_no_prompt(monkeypatch, tmp_path):
    present = tmp_path / "here.mp4"
    present.write_bytes(b"media")
    files = [{"id": "f1", "path": str(present)}]
    clips = [{"id": "c1", "file_id": "f1", "reader": {"path": str(present)}}]

    assert _open_with_missing_media(monkeypatch, tmp_path, files, clips) == []


def test_locating_a_folder_scans_it_off_the_gui_thread(monkeypatch, tmp_path):
    """Review #216: the fingerprint scan and folder walk froze the editor."""
    import threading
    from classes import qt_main_thread

    found = tmp_path / "found"
    found.mkdir()
    (found / "a_roll.mp4").write_bytes(b"x")
    gone = str(tmp_path / "gone" / "a_roll.mp4")
    writers = []

    class _Recorded(dict):
        """Project data that notes which thread writes it."""

        def __setitem__(self, key, value):
            writers.append(threading.current_thread())
            super().__setitem__(key, value)

    files = [_Recorded(id="f1", path=gone)]
    clips = [{"id": "c1", "file_id": "f1", "reader": _Recorded(path=gone)}]

    scan_threads = []
    real_walk = pd.os.walk

    def _walk(*a, **k):
        scan_threads.append(threading.current_thread())
        return real_walk(*a, **k)

    class _Locate:
        AcceptRole = 0
        ActionRole = 1

        def __init__(self, *a, **k):
            self._locate = object()

        def __getattr__(self, name):
            return lambda *a, **k: None

        def addButton(self, text, role):
            return self._locate if "Locate" in str(text) else object()

        def exec_(self):
            return 0

        def clickedButton(self):
            return self._locate

    class _App:
        window = None

        def _tr(self, s):
            return s

        def get_settings(self):
            return None

    monkeypatch.setattr(pd, "get_app", lambda: _App())
    monkeypatch.setattr(pd, "QMessageBox", _Locate)
    monkeypatch.setattr(pd.QFileDialog, "getExistingDirectory", lambda *a, **k: str(found), raising=False)
    monkeypatch.setattr(path_utils, "remember_media_root", lambda folder: None)
    monkeypatch.setattr(path_utils, "_media_roots", lambda: [])
    monkeypatch.setattr(pd.os, "walk", _walk)
    monkeypatch.setattr(qt_main_thread, "is_gui_thread", lambda: True)
    monkeypatch.setattr(qt_main_thread, "_pump_events", lambda: None, raising=False)

    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store.current_filepath = str(tmp_path / "project.zvn")
    store._data = {"files": files, "clips": clips}
    store.check_if_paths_are_valid()

    assert scan_threads and threading.main_thread() not in scan_threads
    # ...but the project is only written on the calling (GUI) thread: timers
    # keep running during the scan and must never see a half-relinked project.
    assert writers and set(writers) == {threading.main_thread()}
    relinked = str(found / "a_roll.mp4")
    assert files[0]["path"] == relinked
    assert clips[0]["reader"]["path"] == relinked
