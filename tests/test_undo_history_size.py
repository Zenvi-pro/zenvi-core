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


def test_an_undone_delete_puts_the_item_back_where_it_was(editor):
    _timeline(editor, clips=5)
    before = _state(editor)
    middle = editor.clips()[2]["id"]
    editor.app.updates.delete(["clips", {"id": middle}])
    assert editor.manager.actionHistory[-1].index == 2
    editor.undo()
    assert _state(editor) == before
    editor.redo()
    assert editor.clip(middle) is None
    editor.undo()
    assert _state(editor) == before


def test_a_replaced_item_is_undone_in_place(editor):
    """Delete + insert of the same id in one step (how an engine answer replaces a clip)."""
    _timeline(editor, clips=4)
    before = _state(editor)
    target = editor.clips()[1]
    replaced = dict(target, alpha=0.25)
    editor.app.updates.transaction_id = "replace-1"
    try:
        editor.app.updates.delete(["clips", {"id": target["id"]}])
        editor.app.updates.insert(["clips"], replaced)
    finally:
        editor.app.updates.transaction_id = None
    after = _state(editor)
    editor.undo()
    assert _state(editor) == before
    editor.redo()
    assert _state(editor) == after


def test_the_delete_index_survives_a_save_and_reopen(editor):
    _timeline(editor, clips=3)
    before = _state(editor)
    first = editor.clips()[0]["id"]
    editor.app.updates.delete(["clips", {"id": first}])
    saved = json.loads(editor.manager.actionHistory[-1].json())
    assert saved["index"] == 0
    action = UpdateAction()
    action.load_json(json.dumps(saved))
    editor.manager.actionHistory[-1] = action
    editor.undo()
    assert _state(editor) == before


def test_history_limit_saves_whole_undo_steps(editor):
    f = _timeline(editor, clips=2)
    for step in range(3):
        editor.app.updates.transaction_id = f"step-{step}"
        try:
            for i in range(4):
                editor.add_clip(f, position=100.0 + step * 10 + i, end=1.0)
        finally:
            editor.app.updates.transaction_id = None
    editor.manager.save_history(editor.store, 2)
    saved = editor.store._data["history"]["undo"]
    assert [a["transaction"] for a in saved] == ["step-1"] * 4 + ["step-2"] * 4
