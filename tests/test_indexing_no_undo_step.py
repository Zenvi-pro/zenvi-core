"""Indexing results are not the user's edits: they never become undo steps.

Regression (E2E round 2, N4): each file's indexing landed as two
"(update on files)" undo steps (intermediate and final result), so after
importing seven files the first Ctrl+Z took back index metadata instead of the
user's last edit, and wait_until_project_indexed_tool reported ``undoSteps: 1``.
"""

import json

from classes.query import File


def test_save_untracked_writes_the_record_without_an_undo_step(editor):
    fid = editor.add_file("video", duration=4)
    clip_id = editor.add_clip(fid, position=0.0, end=2.0)  # the user's last edit
    steps = len(editor.manager.actionHistory)
    editor.store.has_unsaved_changes = False

    f = File.get(id=fid)
    f.data["ai_metadata"] = {"analyzed": True, "index": {"status": "ready"}}
    f.save_untracked("ai_metadata")

    assert len(editor.manager.actionHistory) == steps
    assert editor.file(fid)["ai_metadata"]["index"]["status"] == "ready"
    assert editor.store.has_unsaved_changes  # saved with the project

    editor.undo()  # Ctrl+Z takes back the clip, not the index
    assert editor.clip(clip_id) is None
    assert editor.file(fid)["ai_metadata"]["index"]["status"] == "ready"


def test_save_untracked_writes_only_the_keys_it_names(editor):
    fid = editor.add_file("video", duration=4)
    before = json.dumps(editor.file(fid), sort_keys=True)
    f = File.get(id=fid)
    f.data["ai_metadata"] = {"analyzed": True}
    f.save_untracked("ai_metadata")
    record = dict(editor.file(fid))
    assert record.pop("ai_metadata") == {"analyzed": True}
    assert json.dumps(record, sort_keys=True) == json.dumps(
        {k: v for k, v in json.loads(before).items() if k != "ai_metadata"}, sort_keys=True)


def test_save_untracked_ignores_a_file_not_in_the_project(editor):
    steps, files = len(editor.manager.actionHistory), json.dumps(editor.get("files"))
    f = File()
    f.data = {"path": "/nowhere.mp4"}
    f.save_untracked("ai_metadata")
    assert len(editor.manager.actionHistory) == steps
    assert json.dumps(editor.get("files")) == files


def test_waiting_for_indexing_reports_no_undo_step(editor, monkeypatch):
    from classes import tool_handlers

    fids = [editor.add_file("video", duration=4) for _ in range(3)]

    def index_lands(fid, _files_model, **_kw):
        f = File.get(id=fid)
        f.data["ai_metadata"] = {"analyzed": True}
        f.save_untracked("ai_metadata")
        return ""

    monkeypatch.setattr(tool_handlers, "_wait_for_file_indexing", index_lands)
    receipt = editor.call_receipt("wait_until_project_indexed_tool")
    assert receipt["undoSteps"] == 0, receipt
    assert "3 project file(s) indexed" in receipt["summary"]
    assert editor.undo_steps_since_mark() == 0
    assert all(editor.file(fid)["ai_metadata"]["analyzed"] for fid in fids)
