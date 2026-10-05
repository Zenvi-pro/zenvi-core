"""The Premiere handoff tools (classes.editor_tools.handoff_premiere) through execute_tool, and the menus.

Send To talks to handoff_fakes.FakeHost acting as Premiere's Zenvi Link panel
(loopback MCP, discovery file in the test's user folder). Media is probed by
premiere_fakes.FakeMediaProbe; titles are "rendered" by FakeStills.
"""

import importlib
import json
import os
import pathlib

import pytest

from handoff_fakes import FakeHost, linked, tt, write_discovery  # noqa: F401  (fixtures)
from premiere_fakes import FakeMediaProbe, FakeStills, media_on_disk, video_file

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "premiere"


def _failed(r):
    return r["status"] in ("error", "refused") and r["summary"].startswith("Error") and r["undoSteps"] == 0


@pytest.fixture
def ppro(linked, tmp_path, monkeypatch):
    """The editor with a saved project in tmp, a timeline with one clip, stills faked."""
    from classes.exporters import final_cut_pro as fcp
    linked.store.current_filepath = str(tmp_path / "Trip.zvn")
    linked.app._tr = lambda text: text                   # an untranslated app
    monkeypatch.setattr(fcp, "_render_stills_qt", FakeStills())
    linked.window.timeline._get_transition_reader_json = lambda path: {
        "path": path, "has_single_image": True, "type": "QtImageReader"}
    return linked


def _timeline(editor):
    f = editor.add_file("video")
    editor.add_clip(f, position=0.0, end=4.0)
    editor.mark()


class PremiereHost(FakeHost):
    """Zenvi Link in Premiere: premiere_import_xml answers with the new sequence."""

    def __init__(self, refuse=False):
        super().__init__(app="premiere")
        self.refuse = refuse

    def call(self, name, args):
        if name != "premiere_import_xml":
            return super().call(name, args)
        if self.refuse:
            receipt = {"contract": 3, "status": "refused", "tool": name, "host": "premiere",
                       "summary": "Error: no project is open", "error": {"code": "NO_PROJECT", "message": "x"}}
            return {"content": [{"type": "text", "text": json.dumps(receipt)}], "structuredContent": receipt,
                    "isError": True}
        receipt = {"contract": 3, "status": "applied", "tool": name, "host": "premiere",
                   "summary": "Imported \"Trip.xml\": \"Trip\" (1920x1080, 30 fps, 4 s), opened",
                   "data": {"sequences": [{"sequence_id": "seq-7", "name": "Trip", "duration": 4.0}], "items": 2,
                            "offline": [{"node_id": "n1", "name": "sample_video.mp4"}], "bin": "Zenvi Imports",
                            "created_bins": ["Zenvi Imports"]},
                   "warnings": ["1 media item offline (missing at the XML's paths): sample_video.mp4"],
                   "undoSteps": 2}
        return {"content": [{"type": "text", "text": json.dumps(receipt)}], "structuredContent": receipt,
                "isError": False}


@pytest.fixture
def premiere(ppro):
    host = PremiereHost()
    write_discovery(ppro.user_path, host, app="premiere")
    yield host
    host.stop()


# --- export_to_premiere_tool -------------------------------------------------------------------------------

def test_export_writes_next_to_the_project_without_an_undo_step(ppro, tmp_path):
    _timeline(ppro)
    r = ppro.call_receipt("export_to_premiere_tool")
    assert r["status"] == "applied" and r["undoSteps"] == 0, r["summary"]
    data = r["data"]
    assert data["path"] == str(tmp_path / "Trip (Premiere).xml") and os.path.isfile(data["path"])
    assert data["counts"]["clips"] == 1 and data["replaced"] is False
    assert "Exported the timeline for Premiere Pro" in r["summary"]
    again = ppro.call_receipt("export_to_premiere_tool")
    assert again["data"]["path"] == str(tmp_path / "Trip (Premiere) 2.xml")    # never clobbers the first
    assert ppro.undo_steps_since_mark() == 0


def test_export_refuses_existing_files_unless_overwrite(ppro, tmp_path):
    _timeline(ppro)
    target = tmp_path / "Cut.xml"
    target.write_text("keep me")
    r = ppro.call_receipt("export_to_premiere_tool", output_path=str(target))
    assert _failed(r) and "overwrite=true" in r["summary"] and target.read_text() == "keep me"
    r = ppro.call_receipt("export_to_premiere_tool", output_path=str(target), overwrite=True)
    assert r["status"] == "applied" and r["data"]["replaced"] is True
    assert target.read_text().startswith("<?xml")
    r = ppro.call_receipt("export_to_premiere_tool", output_path=str(tmp_path / "new folder" / "x"))
    assert r["status"] == "applied" and r["data"]["path"] == str(tmp_path / "new folder" / "x.xml")  # folder made
    r = ppro.call_receipt("export_to_premiere_tool", output_path=str(target / "x.xml"))
    assert _failed(r) and "is a file, not a folder" in r["summary"]


def test_export_refuses_an_empty_timeline(ppro):
    r = ppro.call_receipt("export_to_premiere_tool")
    assert _failed(r) and "no clips" in r["summary"]


def test_export_can_collect_media(ppro, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (disk,) = media_on_disk(media, video_file("", "/x/holiday.mp4", duration=10.0))
    f = ppro.add_file("video", path=disk["path"])
    ppro.add_clip(f, end=3.0)
    r = ppro.call_receipt("export_to_premiere_tool", output_path=str(tmp_path / "out" / "Holiday.xml"),
                          collect_media=True)
    assert r["status"] == "applied", r["summary"]
    assert r["data"]["copied_media"] == [str(tmp_path / "out" / "Holiday_media" / "holiday.mp4")]


# --- send_to_premiere_tool -------------------------------------------------------------------------------------

def test_send_refuses_with_how_to_connect_when_premiere_is_not_running(ppro):
    _timeline(ppro)
    r = ppro.call_receipt("send_to_premiere_tool")
    assert _failed(r) and "Premiere Pro is not connected" in r["summary"] and "Zenvi Link" in r["summary"]


def test_send_exports_into_the_assets_folder_and_imports_in_premiere(ppro, premiere, tmp_path):
    _timeline(ppro)
    r = ppro.call_receipt("send_to_premiere_tool")
    assert r["status"] == "applied" and r["undoSteps"] == 0, r["summary"]
    data = r["data"]
    assert data["sequence_name"] == "Trip" and data["sequences"][0]["sequence_id"] == "seq-7"
    assert data["offline"] == [{"node_id": "n1", "name": "sample_video.mp4"}]
    assert "Opened “Trip” in Premiere Pro" in r["summary"] and "could not find 1 media" in r["summary"]
    assert any("offline" in w for w in data["warnings"])
    xml = pathlib.Path(data["xml"])
    assert xml.is_file() and xml.parent.parent == tmp_path / "Trip_assets" / "premiere"
    call = [c for c in premiere.calls if c.get("method") == "tools/call"][-1]["params"]
    assert call == {"name": "premiere_import_xml", "arguments": {"path": str(xml)}}


def test_send_reports_a_host_refusal_with_the_xml_path(ppro, tmp_path):
    host = PremiereHost(refuse=True)
    write_discovery(ppro.user_path, host, app="premiere")
    try:
        _timeline(ppro)
        r = ppro.call_receipt("send_to_premiere_tool")
    finally:
        host.stop()
    assert _failed(r) and "no project is open" in r["summary"] and ".xml" in r["summary"]


# --- import_timeline_xml_tool ------------------------------------------------------------------------------------

@pytest.fixture
def speed_xml(ppro, tmp_path, monkeypatch):
    from classes.handoff import linked_media
    folder = tmp_path / "media"
    folder.mkdir()
    (disk,) = media_on_disk(folder, video_file("", "/media/speed/clip.mp4", duration=20.0))
    xml = tmp_path / "Speeds.xml"
    xml.write_text((FIXTURES / "premiere_speed.xml").read_text().replace("/media/speed/clip.mp4", disk["path"]))
    monkeypatch.setattr(linked_media, "probe_media", FakeMediaProbe({disk["path"]: disk}))
    return str(xml)


def test_import_tool_is_one_undo_step(ppro, speed_xml):
    ppro.mark()
    r = ppro.call_receipt("import_timeline_xml_tool", path=speed_xml)
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    data = r["data"]
    assert len(data["timeline_clip_ids"]) == 3 and len(data["layers"]) == 1 and data["missing_media"] == []
    assert data["sequence_name"] == "Speeds" and "Imported 3 clip(s)" in r["summary"]
    assert ppro.undo_steps_since_mark() == 1
    ppro.undo()
    assert not [c for c in ppro.clips() if c["id"] in data["timeline_clip_ids"]]


def test_import_lands_as_one_preview_batch(ppro, speed_xml, monkeypatch):
    """Playback caching stays off while the clips land and the preview redraws once (not once per clip)."""
    from classes.importers import final_cut_pro as imp
    emit = ppro.window.IgnoreUpdates.emit
    emit.reset_mock()
    r = ppro.call_receipt("import_timeline_xml_tool", path=speed_xml)
    assert r["status"] == "applied" and len(r["data"]["timeline_clip_ids"]) == 3, r["summary"]
    assert [c.args for c in emit.call_args_list] == [(True, False), (False, False)]
    emit.reset_mock()                                    # a commit that fails still ends the batch

    def broken(plan):
        raise RuntimeError("disk full")

    monkeypatch.setattr(imp, "_commit", broken)
    with pytest.raises(RuntimeError):
        imp.commit_import(imp.plan_import(speed_xml))
    assert [c.args for c in emit.call_args_list] == [(True, False), (False, False)]


def test_import_tool_at_playhead(ppro, speed_xml):
    ppro.window.preview_thread.player.Position.return_value = 301      # 10 s at 30 fps
    r = ppro.call_receipt("import_timeline_xml_tool", path=speed_xml, placement="at_playhead")
    assert r["status"] == "applied" and r["data"]["offset"] == pytest.approx(10.0)
    first = min(float(c["position"]) for c in ppro.clips() if c["id"] in r["data"]["timeline_clip_ids"])
    assert first == pytest.approx(10.0)


def test_import_tool_refusals_leave_history_alone(ppro, tmp_path):
    ppro.mark()
    r = ppro.call_receipt("import_timeline_xml_tool", path=str(tmp_path / "nope.xml"))
    assert _failed(r) and "no XML file" in r["summary"]
    junk = tmp_path / "junk.xml"
    junk.write_text("<fcpxml version='1.9'/>")
    r = ppro.call_receipt("import_timeline_xml_tool", path=str(junk))
    assert _failed(r) and "not a Final Cut Pro 7" in r["summary"]
    lost = tmp_path / "lost.xml"
    lost.write_text((FIXTURES / "premiere_speed.xml").read_text().replace("/media/speed/", "/nowhere/at/all/"))
    r = ppro.call_receipt("import_timeline_xml_tool", path=str(lost))
    assert _failed(r) and "missing media clip.mp4" in r["summary"]
    r = ppro.call_receipt("import_timeline_xml_tool", path=str(junk), placement="sideways")
    assert r["status"] in ("error", "refused") and "must be one of" in r["summary"]
    assert ppro.undo_steps_since_mark() == 0


# --- menus ----------------------------------------------------------------------------------------------------------

def test_the_plugin_registers_export_import_and_send_entries():
    from classes.handoff import premiere, ui_registry
    importlib.reload(premiere)
    export = {a.id: a for a in ui_registry.export_actions()}["premiere"]
    imp = {a.id: a for a in ui_registry.import_actions()}["premiere_xml"]
    send = {a.id: a for a in ui_registry.send_actions()}["premiere"]
    assert (export.label, export.order) == ("Premiere Pro (.xml)...", 20)
    assert (imp.label, imp.order) == ("Premiere Pro XML...", 20)
    assert (send.label, send.host_app, send.order) == ("Premiere Pro", "premiere", 20)


def test_send_folder_lives_with_the_project(tmp_path):
    import datetime
    from classes.handoff.premiere import send_folder
    folder = send_folder("Trip: cut/2", str(tmp_path / "Trip.zvn"), now=datetime.datetime(2026, 10, 5, 9, 30, 0))
    assert folder == str(tmp_path / "Trip_assets" / "premiere" / "20261005-093000 Trip_ cut_2")


# --- the menu flows (background jobs, then GUI callbacks; inline under the stub) ------------------------------------

class Boxes:
    """Records QMessageBox.information / warning calls."""

    def __init__(self):
        self.calls = []

    def information(self, parent, title, text):
        self.calls.append(("information", title, text))

    def warning(self, parent, title, text):
        self.calls.append(("warning", title, text))


@pytest.fixture
def boxes(monkeypatch):
    import qt_api
    fake = Boxes()
    monkeypatch.setattr(qt_api, "QMessageBox", fake, raising=False)
    return fake


def _shown(editor):
    return [c.args[0] for c in editor.window.handoff_status.show_message.call_args_list]


def test_menu_export_runs_in_the_background(ppro, tmp_path, boxes):
    from classes.exporters import final_cut_pro as fcp
    _timeline(ppro)
    job = fcp.run_export_job(ppro.window, str(tmp_path / "Menu.xml"), title="Export XML")
    result = job.wait(30)
    assert os.path.isfile(result.path) and _shown(ppro) == ["Exported Menu.xml"]
    assert boxes.calls == [] and ppro.undo_steps_since_mark() == 0


def test_menu_import_is_one_undo_step(ppro, speed_xml, boxes):
    from classes.importers import final_cut_pro as imp
    ppro.mark()
    job = imp.run_import_job(ppro.window, speed_xml, prompt=False)
    plan = job.wait(30)
    assert ppro.undo_steps_since_mark() == 1                            # the whole import, one Undo
    assert _shown(ppro) == ["Imported 3 clip(s) from Speeds"]
    assert boxes.calls and boxes.calls[0][0] == "information"           # the warnings report
    assert "variable speed" in boxes.calls[0][2] and plan.sequence_name == "Speeds"
    ppro.undo()
    assert not [c for c in ppro.clips() if c.get("title") in ("Fast", "Back", "Ramp")]


@pytest.fixture
def lost_xml(ppro, tmp_path, monkeypatch):
    """premiere_speed.xml whose media is not at its path (an XML from another computer), and where it is."""
    from classes.handoff import linked_media
    folder = tmp_path / "found"
    folder.mkdir()
    (disk,) = media_on_disk(folder, video_file("", "/media/speed/clip.mp4", duration=20.0))
    xml = tmp_path / "Lost.xml"
    xml.write_text((FIXTURES / "premiere_speed.xml").read_text().replace("/media/speed/clip.mp4", "/gone/clip.mp4"))
    monkeypatch.setattr(linked_media, "probe_media", FakeMediaProbe({disk["path"]: disk}))
    return str(xml), disk["path"]


def _submitted_jobs(monkeypatch):
    from classes.handoff import jobs
    seen = []
    real = jobs.submit_job

    def submit(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(jobs, "submit_job", submit)
    return seen


def test_menu_import_offers_to_locate_media_even_when_none_of_it_is_found(ppro, lost_xml, boxes, monkeypatch):
    from classes.importers import final_cut_pro as imp
    xml, found = lost_xml
    asked = []
    monkeypatch.setattr(imp, "_ask_for_missing", lambda window, missing: asked.append(missing) or {missing[0]: found})
    submitted = _submitted_jobs(monkeypatch)
    ppro.mark()
    first = imp.run_import_job(ppro.window, xml, prompt=True)
    with pytest.raises(imp.NoClipsError):
        first.wait(30)
    assert asked == [["/gone/clip.mp4"]] and len(submitted) == 2
    plan = submitted[-1].wait(30)                       # planned again with the file the user pointed at
    assert plan.missing == [] and len(plan.clips) == 3
    assert ppro.undo_steps_since_mark() == 1 and _shown(ppro) == ["Imported 3 clip(s) from Speeds"]
    assert not [c for c in boxes.calls if c[0] == "warning"]


def test_menu_import_says_what_is_missing_when_the_user_finds_nothing(ppro, lost_xml, boxes, monkeypatch):
    from classes.importers import final_cut_pro as imp
    xml, _found = lost_xml
    asked = []
    monkeypatch.setattr(imp, "_ask_for_missing", lambda window, missing: asked.append(missing) or {})
    ppro.mark()
    with pytest.raises(imp.NoClipsError):
        imp.run_import_job(ppro.window, xml, prompt=True).wait(30)
    assert len(asked) == 1 and ppro.undo_steps_since_mark() == 0
    assert boxes.calls == [("warning", "Import XML",
                            "Import failed: no clip of 'Speeds' could be imported: missing media clip.mp4")]


def test_menu_send_reports_the_new_sequence(ppro, premiere, boxes):
    from classes.handoff import premiere as ppro_plugin
    _timeline(ppro)
    job = ppro_plugin.start_send(ppro.window)
    result = job.wait(30)
    assert result["sequence_name"] == "Trip"
    assert _shown(ppro) == ["Opened “Trip” in Premiere Pro"]
    assert boxes.calls and "could not find 1 media file" in boxes.calls[0][2]


def test_menu_send_without_clips_says_so(ppro, boxes):
    from classes.handoff import premiere as ppro_plugin
    assert ppro_plugin.start_send(ppro.window) is None
    assert boxes.calls == [("information", "Send to Premiere Pro", "The timeline has no clips to send.")]
