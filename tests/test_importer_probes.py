"""EDL / Final Cut XML import probes media off the GUI thread, once per file.

Review #216: both importers ran wholly on the Qt GUI thread and opened every
clip's source with openshot.Clip there -- again for each clip cut from the
same file -- so a large EDL or XML left the editor unresponsive.
"""

import json
import threading
import types

from unittest.mock import MagicMock

import pytest

from classes import qt_main_thread


class _Record:
    saved = []
    _n = 0

    def __init__(self):
        self.data = {}
        type(self)._n += 1
        self.id = "%s%d" % (type(self).__name__, type(self)._n)

    def save(self):
        self.data.setdefault("id", self.id)  # as the real query objects do
        type(self).saved.append(self)


class _Track(_Record):
    saved = []


class _Clip(_Record):
    saved = []


class _File(_Record):
    saved = []

    @classmethod
    def get(cls, **kwargs):
        return next((f for f in cls.saved if f.data.get("path") == kwargs.get("path")), None)


@pytest.fixture
def importers():
    # Imported when the test runs, not at collection: importing them early binds
    # windows.views.find_file before other test modules patch the app.
    from classes.importers import edl, final_cut_pro
    return edl, final_cut_pro


@pytest.fixture
def probes(monkeypatch, importers):
    """Patch the importers' project + libopenshot; record where each probe ran."""
    edl_importer, fcp_importer = importers
    seen = []

    class _OpenShotClip:
        def __init__(self, path):
            seen.append((path, threading.current_thread()))
            self.path = path

        def Json(self):
            return json.dumps({"id": "x", "reader": {"path": self.path}})

        def Reader(self):
            return types.SimpleNamespace(Json=lambda: json.dumps(
                {"path": self.path, "media_type": "video", "has_video": True, "has_audio": True}))

    project = types.SimpleNamespace(
        get=lambda key, default=None: {"fps": {"num": 24, "den": 1}, "layers": [{"number": 1}],
                                       "width": 1920, "height": 1080}.get(key, default),
        current_filepath="")
    app = types.SimpleNamespace(project=project, _tr=lambda s: s, window=MagicMock())
    for module in (edl_importer, fcp_importer):
        monkeypatch.setattr(module, "get_app", lambda: app)
        monkeypatch.setattr(module, "find_missing_file", lambda path, prompt=True: (path, False, False))
        monkeypatch.setattr(module, "File", _File)
        monkeypatch.setattr(module, "Clip", _Clip)
        monkeypatch.setattr(module, "Track", _Track)
        monkeypatch.setattr(module, "openshot", MagicMock(Clip=_OpenShotClip, SCALE_FIT=1, SCALE_STRETCH=2,
                                                          SCALE_CROP=0, SCALE_NONE=3, CONSTANT=2,
                                                          LINEAR=1, BEZIER=0))
    for record in (_Track, _Clip, _File):
        monkeypatch.setattr(record, "saved", [])
    # Called on the GUI thread, as File > Import does.
    monkeypatch.setattr(qt_main_thread, "is_gui_thread", lambda: True)
    monkeypatch.setattr(qt_main_thread, "_pump_events", lambda: None)
    return seen


def _edl_row(n, src_in, src_out, rec_in, rec_out):
    return ("%03d  AX       V     C        %s %s %s %s\n* SOURCE FILE: clip.mp4\n"
            % (n, src_in, src_out, rec_in, rec_out))


def test_edl_probes_each_source_once_off_the_gui_thread(tmp_path, probes, importers):
    edl_importer = importers[0]
    (tmp_path / "clip.mp4").write_bytes(b"x")
    edl = tmp_path / "cut.edl"
    edl.write_text(
        "TITLE: Cut\nFCM: NON-DROP FRAME\n\n"
        + _edl_row(1, "00:00:00:00", "00:00:01:00", "00:00:00:00", "00:00:01:00")
        + _edl_row(2, "00:00:02:00", "00:00:03:00", "00:00:01:00", "00:00:02:00")
        + _edl_row(3, "00:00:04:00", "00:00:05:00", "00:00:02:00", "00:00:03:00"),
        encoding="utf-8")
    summary = edl_importer.import_edl(str(edl), prompt=False)
    assert len(summary["clip_ids"]) == 3
    assert [p for p, _t in probes] == [str(tmp_path / "clip.mp4")]
    assert probes[0][1] is not threading.main_thread()
    # Each clip got its own copy of the probed clip JSON.
    assert len({id(c.data) for c in _Clip.saved}) == 3


def test_fcp_xml_probes_each_source_once_off_the_gui_thread(tmp_path, probes, importers):
    fcp_importer = importers[1]
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    items = "".join(
        '<clipitem id="c%d"><name>c%d</name><start>%d</start><end>%d</end><in>0</in><out>24</out>'
        '<file id="f1"><pathurl>%s</pathurl></file></clipitem>' % (i, i, i * 24, i * 24 + 24, media)
        for i in range(3))
    xml = tmp_path / "cut.xml"
    xml.write_text('<?xml version="1.0"?><xmeml version="4"><sequence><media><video><track>'
                   + items + '</track></video></media></sequence></xmeml>', encoding="utf-8")
    summary = fcp_importer.import_xml(str(xml), prompt=False)
    assert len(summary["clip_ids"]) == 3
    assert [p for p, _t in probes] == [str(media)]
    assert probes[0][1] is not threading.main_thread()
