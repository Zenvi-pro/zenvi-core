"""project-export: project lifecycle, recent/recovery, EDL/XML, media, caches (classes.editor_tools.project_export)."""

import json
import os
import sys
import time
import types
import zipfile
from unittest.mock import MagicMock

import pytest

from classes import info


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


class _Settings:
    """Enough of SettingStore for the lifecycle tools."""

    class actionType:
        IMPORT, EXPORT, LOAD, SAVE = range(4)

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.saved = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save(self):
        self.saved += 1

    def setDefaultPath(self, *_a):
        pass

    def getDefaultPath(self, *_a):
        return self.values.get("export-folder", "")


@pytest.fixture
def settings(editor):
    s = _Settings({"recent_projects": []})
    editor.app.get_settings.return_value = s
    editor.app.get_settings.side_effect = None
    return s


def _fake_save(editor):
    def save_project(path, raise_errors=False):
        assert raise_errors is True
        with open(path, "w") as fh:
            fh.write("{}")
        editor.store.current_filepath = path
        editor.store.has_unsaved_changes = False
    editor.window.save_project = MagicMock(side_effect=save_project)


def _dirty(editor):
    f = editor.add_file("video")
    editor.add_clip(f)
    editor.store.has_unsaved_changes = True


# --- info -----------------------------------------------------------------

def test_project_info_reports_format_counts_and_dirty_flag(editor):
    f = editor.add_file("video", duration=8.0)
    editor.add_clip(f, position=2.0, end=5.0)
    editor.store.has_unsaved_changes = True
    data = _receipt(editor.call("get_project_info_tool"))
    assert (data["width"], data["height"], data["fps"]) == (1920, 1080, 30.0)
    assert data["aspect"] == "16:9" and data["orientation"] == "landscape"
    assert data["saved"] is False and data["path"] is None and data["unsaved_changes"] is True
    assert data["duration_seconds"] == 7.0
    assert data["counts"]["clips"] == 1 and data["counts"]["files"] == 1
    assert data["sample_rate"] == 48000 and data["channel_layout"] == "stereo"


# --- new ------------------------------------------------------------------

def test_new_project_refuses_to_discard_unsaved_work(editor):
    _dirty(editor)
    out = editor.call("new_project_tool")
    assert out.startswith("Error") and "unsaved changes" in out
    editor.window.new_project.assert_not_called()
    assert editor.undo_steps_since_mark() == 0


def test_new_project_save_first_needs_a_saved_project(editor):
    _dirty(editor)
    out = editor.call("new_project_tool", save_first=True)
    assert out.startswith("Error") and "never been saved" in out
    editor.window.new_project.assert_not_called()


def test_new_project_saves_first_then_starts_fresh(editor, tmp_path):
    _dirty(editor)
    editor.store.current_filepath = str(tmp_path / "a.zvn")
    _fake_save(editor)
    data = _receipt(editor.call("new_project_tool", save_first=True))
    editor.window.save_project.assert_called_once()
    editor.window.new_project.assert_called_once()
    assert data["saved_previous_to"] == str(tmp_path / "a.zvn")


def test_new_project_discard_and_unknown_profile_is_refused_before_anything(editor):
    _dirty(editor)
    out = editor.call("new_project_tool", discard_unsaved=True, profile="no such format zz")
    assert out.startswith("Error") and "no profile" in out
    editor.window.new_project.assert_not_called()
    out = editor.call("new_project_tool", discard_unsaved=True)
    assert not out.startswith("Error"), out
    editor.window.new_project.assert_called_once()


# --- open -----------------------------------------------------------------

def test_open_project_validates_before_touching_anything(editor, tmp_path):
    assert "not found" in editor.call("open_project_tool", file_path=str(tmp_path / "nope.zvn"))
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    assert "not a project file" in editor.call("open_project_tool", file_path=str(media))
    assert "give file_path" in editor.call("open_project_tool")
    editor.window.open_project.assert_not_called()


def test_open_project_refuses_when_dirty_and_opens_non_interactively(editor, tmp_path):
    proj = tmp_path / "b.zvn"
    proj.write_text("{}")
    _dirty(editor)
    out = editor.call("open_project_tool", file_path=str(proj))
    assert out.startswith("Error") and "discard_unsaved" in out
    editor.window.open_project.assert_not_called()

    editor.store.last_missing_media = ["/gone/a.mp4"]
    editor.window.open_project.return_value = True
    data = _receipt(editor.call("open_project_tool", file_path=str(proj), discard_unsaved=True))
    editor.window.open_project.assert_called_once_with(str(proj), interactive=False)
    assert data["missing_media"] == ["/gone/a.mp4"] and data["missing_count"] == 1


def test_open_project_reports_a_failed_load(editor, tmp_path):
    proj = tmp_path / "c.zvn"
    proj.write_text("{}")
    editor.window.open_project.side_effect = ValueError("corrupt json")
    out = editor.call("open_project_tool", file_path=str(proj))
    assert out.startswith("Error") and "corrupt json" in out
    editor.window.open_project.side_effect = None
    editor.window.open_project.return_value = False
    assert editor.call("open_project_tool", file_path=str(proj)).startswith("Error")


def test_open_recent_project_by_index(editor, settings, tmp_path):
    older, newer = tmp_path / "old.zvn", tmp_path / "new.zvn"
    older.write_text("{}")
    newer.write_text("{}")
    settings.values["recent_projects"] = [str(older), str(newer)]
    editor.window.open_project.return_value = True
    _receipt(editor.call("open_project_tool", recent_index=1))
    editor.window.open_project.assert_called_once_with(str(newer), interactive=False)
    assert "only 2 recent" in editor.call("open_project_tool", recent_index=3)


# --- save -----------------------------------------------------------------

def test_save_needs_a_path_the_first_time(editor):
    out = editor.call("save_project_tool")
    assert out.startswith("Error") and "never been saved" in out


def test_save_as_adds_the_extension_and_refuses_to_overwrite(editor, tmp_path, settings):
    _fake_save(editor)
    target = tmp_path / "sub" / "My Edit"
    data = _receipt(editor.call("save_project_tool", file_path=str(target)))
    assert data["path"] == str(target) + ".zvn" and os.path.isfile(data["path"])
    assert editor.undo_steps_since_mark() == 0
    other = tmp_path / "other.zvn"
    other.write_text("{}")
    out = editor.call("save_project_tool", file_path=str(other))
    assert out.startswith("Error") and "overwrite" in out
    _receipt(editor.call("save_project_tool", file_path=str(other), overwrite=True))
    # Saving again to the current file needs no overwrite flag.
    data = _receipt(editor.call("save_project_tool"))
    assert data["path"] == str(other) and data["save_as"] is False


def test_save_failure_is_an_error_not_success(editor, tmp_path):
    editor.window.save_project = MagicMock(side_effect=PermissionError("read-only volume"))
    out = editor.call("save_project_tool", file_path=str(tmp_path / "x.zvn"))
    assert out.startswith("Error") and "read-only volume" in out


# --- recent ---------------------------------------------------------------

def test_recent_projects_list_and_forget(editor, settings, tmp_path):
    kept = tmp_path / "kept.zvn"
    kept.write_text("{}")
    gone = str(tmp_path / "gone.zvn")
    settings.values["recent_projects"] = [gone, str(kept)]
    data = _receipt(editor.call("list_recent_projects_tool"))
    assert [p["name"] for p in data["projects"]] == ["kept", "gone"]
    assert [p["exists"] for p in data["projects"]] == [True, False]

    data = _receipt(editor.call("forget_recent_projects_tool", missing_only=True))
    assert data["forgotten"] == [gone] and settings.values["recent_projects"] == [str(kept)]
    assert os.path.exists(kept), "forgetting never deletes files"
    assert "not in the recent" in editor.call("forget_recent_projects_tool", file_path=gone)
    _receipt(editor.call("forget_recent_projects_tool"))
    assert settings.values["recent_projects"] == []
    assert editor.call("forget_recent_projects_tool").startswith("Error")


# --- recovery -------------------------------------------------------------

def _zip(path, inner):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner, "{}")


@pytest.fixture
def recovery_dir(tmp_path, monkeypatch):
    d = tmp_path / "recovery"
    d.mkdir()
    monkeypatch.setattr(info, "RECOVERY_PATH", str(d))
    return d


def test_recovery_versions_match_the_project_name_exactly(editor, recovery_dir, tmp_path):
    editor.store.current_filepath = str(tmp_path / "trip.zvn")
    now = int(time.time())
    for name in (f"{now - 600}-trip.zip", f"{now - 60}-trip.zip", f"{now}-trip-2.zip"):
        _zip(recovery_dir / name, "trip.zvn")
    with zipfile.ZipFile(recovery_dir / f"{now - 30}-trip.zip", "w"):
        pass  # empty copy written by an older first save: not restorable, not listed
    data = _receipt(editor.call("list_recovery_versions_tool"))
    assert [v["version"] for v in data["versions"]] == [1, 2]
    assert data["versions"][0]["path"].endswith(f"{now - 60}-trip.zip")


def test_restore_recovery_version(editor, recovery_dir, tmp_path):
    proj = tmp_path / "trip.zvn"
    proj.write_text("{}")
    editor.store.current_filepath = str(proj)
    _zip(recovery_dir / f"{int(time.time())}-trip.zip", "trip.zvn")
    assert "only 1" in editor.call("restore_recovery_version_tool", version=2)
    editor.store.has_unsaved_changes = True
    assert "unsaved changes" in editor.call("restore_recovery_version_tool")
    editor.window.restore_recovery_file.return_value = (str(proj), str(tmp_path / "trip-1-backup.zvn"))
    editor.window.open_project.return_value = True
    data = _receipt(editor.call("restore_recovery_version_tool", discard_unsaved=True))
    editor.window.open_project.assert_called_once_with(str(proj), interactive=False)
    assert data["backup_of_previous"].endswith("trip-1-backup.zvn")


def test_recovery_needs_a_saved_project(editor):
    data = _receipt(editor.call("list_recovery_versions_tool"))
    assert data["versions"] == []
    assert editor.call("restore_recovery_version_tool").startswith("Error")


# --- EDL / XML ------------------------------------------------------------

def test_import_edl_refuses_when_no_media_can_be_found(editor, tmp_path):
    edl = tmp_path / "cut.edl"
    edl.write_text("TITLE: cut\n001  AX  V  C  00:00:00:00 00:00:01:00 00:00:00:00 00:00:01:00\n"
                   "* FROM CLIP NAME: /definitely/missing/a.mp4\n")
    out = editor.call("import_project_file_tool", file_path=str(edl))
    assert out.startswith("Error") and "none of the media" in out
    assert editor.undo_steps_since_mark() == 0


def test_import_edl_goes_through_the_importer_as_one_undo_step(editor, tmp_path, monkeypatch):
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    edl = tmp_path / "cut.edl"
    edl.write_text(f"TITLE: cut\n* FROM CLIP NAME: {media}\n")
    from classes.importers import edl as edl_mod
    f = editor.add_file("video")

    def fake_import(path, prompt=True):
        assert path == str(edl) and prompt is False
        from classes.query import Clip, Track
        t = Track()
        t.data = {"number": 9000000, "y": 0, "label": "EDL Import", "lock": False}
        t.save()
        c = Clip()
        c.data = dict(editor.clip(editor.add_clip(f)) or {})
        c.data.pop("id", None)
        c.data["layer"] = 9000000
        c.save()
        return {"track_number": 9000000, "clip_ids": [c.id], "missing": ["/x/missing.mov"]}

    monkeypatch.setattr(edl_mod, "import_edl", fake_import)
    editor.mark()
    data = _receipt(editor.call("import_project_file_tool", file_path=str(edl)))
    assert data["format"] == "edl" and len(data["timeline_clip_ids"]) == 1
    assert data["missing_media"] == ["/x/missing.mov"] and data["layers"] == [9000000]
    assert editor.undo_steps_since_mark() == 1


def test_import_xml_validates_the_file(editor, tmp_path):
    bad = tmp_path / "x.xml"
    bad.write_text("<xmeml><sequence/></xmeml>")
    assert "no clips" in editor.call("import_project_file_tool", file_path=str(bad))
    junk = tmp_path / "y.xml"
    junk.write_text("not xml <")
    assert "not valid XML" in editor.call("import_project_file_tool", file_path=str(junk))
    other = tmp_path / "z.txt"
    other.write_text("x")
    assert "cannot tell the format" in editor.call("import_project_file_tool", file_path=str(other))


def test_export_fcpxml_and_edl(editor, tmp_path, monkeypatch):
    assert "no clips" in editor.call("export_project_file_tool", file_path=str(tmp_path / "a.xml"))
    editor.add_clip(editor.add_file("video"))
    written = []
    fake_fcp = types.ModuleType("classes.exporters.final_cut_pro")
    fake_fcp.export_xml = lambda path: (open(path, "w").write("<xmeml/>"), written.append(path), path)[-1]
    fake_edl = types.ModuleType("classes.exporters.edl")

    def export_edl(path):
        out = path[:-4] + "-Track 1.edl"
        open(out, "w").write("TITLE: x\n")
        return [out]
    fake_edl.export_edl = export_edl
    monkeypatch.setitem(sys.modules, "classes.exporters.final_cut_pro", fake_fcp)
    monkeypatch.setitem(sys.modules, "classes.exporters.edl", fake_edl)

    data = _receipt(editor.call("export_project_file_tool", file_path=str(tmp_path / "cut")))
    assert data["files"] == [str(tmp_path / "cut.xml")]
    assert "already exists" in editor.call("export_project_file_tool", file_path=str(tmp_path / "cut.xml"))
    data = _receipt(editor.call("export_project_file_tool", format="edl", file_path=str(tmp_path / "cut.edl")))
    assert data["files"] == [str(tmp_path / "cut-Track 1.edl")]
    assert "already exists" in editor.call("export_project_file_tool", format="edl",
                                           file_path=str(tmp_path / "cut.edl"))
    assert editor.undo_steps_since_mark() == 0


# --- media collect / reclaim ----------------------------------------------

def test_collect_then_reclaim_media(editor, tmp_path):
    outside = tmp_path / "outside" / "shot.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"video-bytes")
    fid = editor.add_file("video", path=str(outside))
    cid = editor.add_clip(fid)
    assert "save the project first" in editor.call("consolidate_project_media_tool", action="collect")

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    editor.store.current_filepath = str(proj_dir / "film.zvn")
    editor.mark()
    data = _receipt(editor.call("consolidate_project_media_tool", action="collect"))
    new_path = editor.file(fid)["path"]
    assert data["count"] == 1 and new_path != str(outside) and os.path.isfile(new_path)
    assert editor.file(fid)["original_path"] == str(outside)
    assert editor.clip(cid)["reader"]["path"] == new_path
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.file(fid)["path"] == str(outside)
    editor.redo()
    assert editor.file(fid)["path"] == new_path

    editor.mark()
    data = _receipt(editor.call("consolidate_project_media_tool", action="reclaim"))
    assert data["count"] == 1 and not os.path.exists(new_path)
    assert editor.file(fid)["path"] == str(outside) and editor.clip(cid)["reader"]["path"] == str(outside)
    assert editor.undo_steps_since_mark() == 0, "reclaim deleted the copies, so it must not be undoable"
    data = _receipt(editor.call("consolidate_project_media_tool", action="reclaim"))
    assert data["count"] == 0


# --- caches and history ---------------------------------------------------

def test_playback_cache_flush_adds_no_history(editor):
    _receipt(editor.call("reset_caches_and_history_tool", target="playback_cache"))
    editor.window.actionClearAllCache_trigger.assert_called_once()
    assert editor.undo_steps_since_mark() == 0


def test_drop_waveforms_is_one_undo_step(editor):
    fid = editor.add_file("audio", ui={"audio_data": [0.1, 0.5], "audio_data_rate": 20})
    cid = editor.add_clip(fid, ui={"audio_data": [0.1, 0.5]})
    data = _receipt(editor.call("reset_caches_and_history_tool", target="waveforms"))
    assert (data["files"], data["clips"]) == (1, 1)
    assert "audio_data" not in editor.file(fid)["ui"] and "audio_data" not in editor.clip(cid)["ui"]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.file(fid)["ui"]["audio_data"] == [0.1, 0.5]
    editor.mark()
    editor.redo()
    editor.mark()
    data = _receipt(editor.call("reset_caches_and_history_tool", target="waveforms"))
    assert data["files"] == 0 and editor.undo_steps_since_mark() == 0


def test_clearing_undo_history_needs_confirmation(editor):
    editor.add_clip(editor.add_file("video"))
    clip = editor.clips()[0]
    from classes.query import Clip
    c = Clip.get(id=clip["id"])
    c.data = {"position": 3.0}
    c.save()
    assert len(editor.manager.actionHistory) > 0
    out = editor.call("reset_caches_and_history_tool", target="undo_history")
    assert out.startswith("Error") and "confirm" in out
    data = _receipt(editor.call("reset_caches_and_history_tool", target="undo_history", confirm=True))
    assert data["undo_steps_cleared"] >= 1 and len(editor.manager.actionHistory) == 0
    assert editor.store.has_unsaved_changes is True


def test_edl_files_are_named_after_the_real_track_numbers(editor, tmp_path):
    """Regression: a countdown that skipped empty tracks named UI track 3 'TRACK 5'."""
    f = editor.add_file("video")
    editor.add_clip(f, layer=1000000)
    editor.add_clip(f, layer=3000000)  # tracks 4 and 5 above stay empty
    data = _receipt(editor.call("export_project_file_tool", format="edl", file_path=str(tmp_path / "cut.edl")))
    names = sorted(os.path.basename(p) for p in data["files"])
    assert names == ["cut-TRACK 1.edl", "cut-TRACK 3.edl"]
    body = open(tmp_path / "cut-TRACK 3.edl").read()
    assert body.startswith("TITLE: cut - TRACK 3") and "sample_video.mp4" in body
