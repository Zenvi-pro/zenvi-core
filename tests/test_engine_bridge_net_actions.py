"""An engine answer lands as the fewest desktop updates that make the same project.

The engine answers its edit log: scratch items inserted and deleted again,
updates of items it just inserted, clips replaced by delete + insert. Applied
as is, every step was an undo entry holding a copy of a clip (E2E round 2, N1).
"""

import copy
import json

from classes.editor_tools import engine_bridge as bridged


def _none(_name, _id):
    return None


def _clip(cid, **values):
    return {"id": cid, "layer": 1, "position": 0.0, "alpha": 1.0, **values}


def test_a_scratch_item_and_every_action_on_it_are_dropped():
    actions = [{"type": "insert", "key": ["files"], "value": {"id": "SCRATCHF", "path": "/a.wav"}},
               {"type": "delete", "key": ["files", {"id": "SCRATCHF"}]},
               {"type": "insert", "key": ["clips"], "value": _clip("SCRATCHC")},
               {"type": "update", "key": ["clips", {"id": "SCRATCHC"}], "value": {"alpha": 0.5}},
               {"type": "update", "key": ["clips", {"id": "SCRATCHC"}, "effects"], "value": []},
               {"type": "delete", "key": ["clips", {"id": "SCRATCHC"}]},
               {"type": "insert", "key": ["clips"], "value": _clip("FINAL")}]
    assert bridged.net_actions(actions, _none) == [actions[-1]]


def test_an_update_of_an_item_just_inserted_folds_into_its_insert():
    actions = [{"type": "insert", "key": ["clips"], "value": _clip("NEW")},
               {"type": "update", "key": ["clips", {"id": "NEW"}], "value": {"alpha": 0.5}}]
    net = bridged.net_actions(actions, _none)
    assert net == [{"type": "insert", "key": ["clips"], "value": _clip("NEW", alpha=0.5)}]
    assert actions[0]["value"]["alpha"] == 1.0  # the answer itself is untouched


def test_an_update_after_a_deeper_one_does_not_fold():
    actions = [{"type": "insert", "key": ["clips"], "value": _clip("NEW", effects=[])},
               {"type": "update", "key": ["clips", {"id": "NEW"}, "effects"], "value": [{"id": "E1"}]},
               {"type": "update", "key": ["clips", {"id": "NEW"}], "value": {"effects": []}}]
    assert bridged.net_actions(actions, _none) == actions


def test_a_replaced_item_with_the_same_keys_is_an_update_of_what_changed():
    old = _clip("CUT", alpha=1.0)
    actions = [{"type": "delete", "key": ["clips", {"id": "CUT"}]},
               {"type": "insert", "key": ["clips"], "value": _clip("CUT", alpha=0.0)}]
    net = bridged.net_actions(actions, lambda name, cid: old if (name, cid) == ("clips", "CUT") else None)
    assert net == [{"type": "update", "key": ["clips", {"id": "CUT"}], "value": {"alpha": 0.0}}]


def test_a_replacement_identical_to_the_item_is_nothing():
    old = _clip("CUT")
    actions = [{"type": "delete", "key": ["clips", {"id": "CUT"}]},
               {"type": "insert", "key": ["clips"], "value": _clip("CUT")}]
    assert bridged.net_actions(actions, lambda *_: old) == []


def test_a_replacement_that_adds_or_removes_keys_stays_a_delete_and_insert():
    """An update merges and its undo merges back: a key added would survive the undo."""
    old = _clip("CUT")
    for new in (_clip("CUT", zenvi_motion_transitions=["whip"]), {"id": "CUT", "layer": 1}):
        actions = [{"type": "delete", "key": ["clips", {"id": "CUT"}]},
                   {"type": "insert", "key": ["clips"], "value": new}]
        assert bridged.net_actions(actions, lambda *_: old) == actions


def test_an_answer_lands_exactly_and_undoes_and_redoes_exactly(editor):
    """Through the real store: scratch dropped, replacements in place, one undo step, exact both ways."""
    f = editor.add_file("video", duration=10)
    a = editor.add_clip(f, position=0.0, end=2.0)
    b = editor.add_clip(f, position=2.0, start=2.0, end=4.0)
    c = editor.add_clip(f, position=4.0, start=4.0, end=6.0)
    old_a, old_b = editor.clip(a), editor.clip(b)

    def state():
        return json.dumps({k: v for k, v in editor.store._data.items() if k != "history"})

    before = state()
    scratch = dict(old_a, id="SCRATCH")
    actions = [{"type": "insert", "key": ["clips"], "value": scratch},
               {"type": "update", "key": ["clips", {"id": "SCRATCH"}], "value": {"alpha": 0.5}},
               {"type": "delete", "key": ["clips", {"id": "SCRATCH"}]},
               {"type": "delete", "key": ["clips", {"id": a}]},
               {"type": "insert", "key": ["clips"], "value": dict(old_a, position=0.5)},
               {"type": "delete", "key": ["clips", {"id": b}]},
               {"type": "insert", "key": ["clips"], "value": dict(old_b, zenvi_motion_transitions=["whip"])}]
    editor.app.updates.transaction_id = "answer-1"
    try:
        for action in bridged.net_actions(actions, bridged._current_item(editor.store._data)):
            bridged._APPLY[action["type"]](editor.app.updates, action.get("key"), action.get("value"))
    finally:
        editor.app.updates.transaction_id = None

    assert [x["id"] for x in editor.clips()] == [a, c, b]  # a updated in place; b replaced
    assert editor.clip(a)["position"] == 0.5 and editor.clip("SCRATCH") is None
    assert editor.clip(b)["zenvi_motion_transitions"] == ["whip"]
    assert editor.undo_steps_since_mark() == 1
    assert len(editor.manager.actionHistory[editor._mark:]) == 3  # update a, delete b, insert b
    after = state()
    editor.undo()
    assert state() == before
    editor.redo()
    assert state() == after


def _lands_as_answered(editor, actions):
    """Applied net, the project is what the answer applied as is makes (list order aside)."""
    def apply(acts, tid):
        editor.app.updates.transaction_id = tid
        try:
            for action in copy.deepcopy(acts):  # undo appends the id to an insert's key
                bridged._APPLY[action["type"]](editor.app.updates, action.get("key"), action.get("value"))
        finally:
            editor.app.updates.transaction_id = None

    def clips():
        return sorted(editor.clips(), key=lambda x: x["id"])

    net = bridged.net_actions(actions, bridged._current_item(editor.store._data))
    apply(actions, "as-answered")
    answered = clips()
    editor.undo()
    apply(net, "net")
    assert clips() == answered


def test_an_item_updated_before_it_is_replaced_lands_as_answered(editor):
    """The replacement's changed keys are against the item as the answer left it, not as it was."""
    f = editor.add_file("video", duration=10)
    a = editor.add_clip(f, position=0.0, end=2.0)
    old = editor.clip(a)
    _lands_as_answered(editor, [
        {"type": "update", "key": ["clips", {"id": a}], "value": {"position": 5.0}},
        {"type": "delete", "key": ["clips", {"id": a}]},
        {"type": "insert", "key": ["clips"], "value": dict(old)}])


def test_an_item_replaced_twice_lands_as_answered(editor):
    f = editor.add_file("video", duration=10)
    a = editor.add_clip(f, position=0.0, end=2.0)
    old = editor.clip(a)
    _lands_as_answered(editor, [
        {"type": "delete", "key": ["clips", {"id": a}]},
        {"type": "insert", "key": ["clips"], "value": dict(old, position=5.0)},
        {"type": "delete", "key": ["clips", {"id": a}]},
        {"type": "insert", "key": ["clips"], "value": dict(old)}])
