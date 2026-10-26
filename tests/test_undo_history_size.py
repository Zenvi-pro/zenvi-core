"""An undo entry is about the size of what changed, not of the list it changed.

An insert into a top-level list (``["clips"]``, ``["files"]``) used to keep a
copy of the whole list as its ``old_values``: with a 57-clip timeline every
added clip stored 1.3 MB, and ten edits took a 175 KB project to 34 MB. Undo
never reads it (an insert is undone by deleting the item by id).
"""

import json

from classes.updates import UpdateAction


def _timeline(editor, clips=60):
    f = editor.add_file("video", duration=600)
    for i in range(clips):
        editor.add_clip(f, position=i * 2.0, end=2.0)
    return f


def _state(editor):
    """The project as saved, without its history."""
    return json.dumps({k: v for k, v in editor.store._data.items() if k != "history"}, sort_keys=True)


def test_an_insert_keeps_no_copy_of_the_list_it_went_into(editor):
    f = _timeline(editor)
    clip_id = editor.add_clip(f, position=200.0, end=2.0)
    action = editor.manager.actionHistory[-1]
    assert action.type == "insert" and action.values["id"] == clip_id
    assert action.old_values is None
    assert len(action.json()) < 2 * len(json.dumps(editor.clip(clip_id)))


def test_ten_inserts_save_a_history_smaller_than_the_timeline_and_undo_redo_exactly(editor):
    f = _timeline(editor)
    before = _state(editor)
    history_start = len(editor.manager.actionHistory)
    for i in range(10):
        editor.add_clip(f, position=300.0 + i * 2, end=2.0)
    after = _state(editor)

    editor.manager.save_history(editor.store, 50)
    history = len(json.dumps(editor.store._data["history"]))
    assert history < len(json.dumps(editor.store._data["clips"])), history

    while len(editor.manager.actionHistory) > history_start:
        editor.undo()
    assert _state(editor) == before
    for _ in range(10):
        editor.redo()
    assert _state(editor) == after


def test_a_history_saved_with_whole_list_copies_loads_compact_and_still_undoes(editor):
    f = _timeline(editor, clips=3)
    clip_id = editor.add_clip(f, position=50.0, end=2.0)
    saved = json.loads(editor.manager.actionHistory[-1].json())
    saved["old_values"] = [dict(c) for c in editor.clips()]  # what older versions wrote

    action = UpdateAction()
    action.load_json(json.dumps(saved))
    assert action.old_values is None
    editor.manager.actionHistory[-1] = action
    editor.undo()
    assert editor.clip(clip_id) is None
    editor.redo()
    assert editor.clip(clip_id) is not None


def test_updates_and_deletes_still_keep_the_item_they_change(editor):
    f = _timeline(editor, clips=2)
    clip_id = editor.add_clip(f, position=50.0, end=2.0)
    original = json.dumps(editor.clip(clip_id), sort_keys=True)
    editor.app.updates.update(["clips", {"id": clip_id}], {"alpha": 0.5})
    assert editor.manager.actionHistory[-1].old_values["id"] == clip_id
    editor.app.updates.delete(["clips", {"id": clip_id}])
    assert editor.manager.actionHistory[-1].old_values["id"] == clip_id
    editor.undo()
    editor.undo()
    assert json.dumps(editor.clip(clip_id), sort_keys=True) == original
