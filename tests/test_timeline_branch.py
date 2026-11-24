"""The agent edits a branch of the timeline; the user applies or discards it (#426)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from classes import project_data as pd, timeline_branch as tb, updates as um
from classes.json_data import JsonDataStore
from classes.update_queue import UpdateQueue, UpdatesRouter


class _App(MagicMock):
    project = tb.RoutedProject()


@pytest.fixture
def world(monkeypatch):
    app = _App()
    monkeypatch.setattr(um, "get_app", lambda: app)
    monkeypatch.setattr(pd, "get_app", lambda: app)
    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)  # skip new(): no default-project file read
    JsonDataStore.__init__(store)
    store.has_unsaved_changes = False
    store._data = {"clips": [], "effects": [], "files": [], "layers": [], "markers": []}
    manager = um.UpdateManager()
    app.updates = UpdatesRouter(manager, UpdateQueue(manager))
    app.project = store
    manager.add_listener(store)
    timeline = MagicMock()
    manager.add_listener(timeline)
    app.updates.insert(["clips"], {"id": "A", "position": 0.0, "start": 0.0, "end": 5.0, "layer": 1})
    app.updates.insert(["clips"], {"id": "B", "position": 5.0, "start": 0.0, "end": 5.0, "layer": 1})
    manager.actionHistory.clear()
    timeline.reset_mock()
    yield app, store, manager, timeline
    tb.discard()


def _clip(store, cid):
    return next((c for c in store._data["clips"] if c["id"] == cid), None)


def test_agent_edits_land_on_the_branch_and_leave_the_live_timeline_alone(world):
    app, store, manager, timeline = world
    branch = tb.start(store)
    with tb.bound(branch):
        app.updates.update(["clips", {"id": "A"}], {"position": 10.0})
        app.updates.delete(["clips", {"id": "B"}])
        assert app.project is branch.store, "tools read the branch while bound"
        assert _clip(app.project, "A")["position"] == 10.0
        assert _clip(app.project, "B") is None
    assert app.project is store
    assert _clip(store, "A")["position"] == 0.0 and _clip(store, "B") is not None, "live data unchanged"
    assert manager.actionHistory == [], "the user's undo history is untouched"
    timeline.changed.assert_not_called()


def test_binding_is_per_thread(world):
    app, store, _, _ = world
    branch = tb.start(store)
    seen = []
    with tb.bound(branch):
        t = threading.Thread(target=lambda: seen.append(app.project))
        t.start()
        t.join()
    assert seen == [store], "a user-facing thread keeps reading the live project"


def test_apply_replays_the_branch_as_one_undo_step(world):
    app, store, manager, _ = world
    branch = tb.start(store)
    with tb.bound(branch):
        app.updates.update(["clips", {"id": "A"}], {"position": 10.0})
        app.updates.insert(["clips"], {"id": "C", "position": 20.0, "start": 0.0, "end": 2.0, "layer": 1})
    skipped = tb.apply_active(app.updates)
    assert skipped == []
    assert _clip(store, "A")["position"] == 10.0 and _clip(store, "C") is not None
    assert tb.active() is None
    assert len({a.transaction for a in manager.actionHistory}) == 1, "one undo step"
    app.window = MagicMock()
    manager.undo()
    assert _clip(store, "A")["position"] == 0.0 and _clip(store, "C") is None


def test_an_object_the_user_also_changed_is_skipped_and_reported(world):
    app, store, _, _ = world
    branch = tb.start(store)
    with tb.bound(branch):
        app.updates.update(["clips", {"id": "A"}], {"position": 10.0})
        app.updates.update(["clips", {"id": "B"}], {"position": 30.0})
    app.updates.update(["clips", {"id": "A"}], {"position": 2.0})  # the user, meanwhile
    skipped = tb.apply_active(app.updates)
    assert [op.key for op in skipped] == [["clips", {"id": "A"}]]
    assert _clip(store, "A")["position"] == 2.0, "the user's edit wins"
    assert _clip(store, "B")["position"] == 30.0, "the rest still applies"


def test_discard_leaves_the_live_project_as_it_was(world):
    app, store, manager, _ = world
    branch = tb.start(store)
    with tb.bound(branch):
        app.updates.delete(["clips", {"id": "A"}])
    tb.discard()
    assert tb.active() is None and _clip(store, "A") is not None and manager.actionHistory == []


def test_undo_inside_the_branch_undoes_only_the_agent(world):
    app, store, manager, _ = world
    app.updates.update(["clips", {"id": "B"}], {"position": 7.0})  # a user edit before the run
    branch = tb.start(store)
    with tb.bound(branch):
        app.updates.update(["clips", {"id": "A"}], {"position": 10.0})
        assert len(app.updates.actionHistory) == 1
        app.updates.undo()
        assert _clip(app.project, "A")["position"] == 0.0
        assert len(app.updates.redoHistory) == 1
        app.updates.redo()
        assert _clip(app.project, "A")["position"] == 10.0
    assert _clip(store, "B")["position"] == 7.0 and len(manager.actionHistory) == 1


def test_query_cache_does_not_mix_branch_and_live_objects(world, monkeypatch):
    app, store, _, _ = world
    from classes import query
    monkeypatch.setattr(query, "get_app", lambda: app)
    branch = tb.start(store)
    assert query.Clip.get(id="A").data["position"] == 0.0
    with tb.bound(branch):
        app.updates.update(["clips", {"id": "A"}], {"position": 10.0})
        assert query.Clip.get(id="A").data["position"] == 10.0
    assert query.Clip.get(id="A").data["position"] == 0.0


def test_opening_another_project_drops_the_branch(world):
    app, store, _, _ = world
    tb.start(store)
    app.updates.load(store._data)
    assert tb.active() is None


def test_undo_and_redo_keep_the_stacks_the_undo_tool_holds(world):
    app, store, _, _ = world
    branch = tb.start(store)
    with tb.bound(branch):
        app.updates.transaction_id = "t1"
        app.updates.update(["clips", {"id": "A"}], {"position": 10.0})
        app.updates.transaction_id = "t2"
        app.updates.update(["clips", {"id": "A"}], {"position": 20.0})
        app.updates.transaction_id = None
        undo_stack, redo_stack = app.updates.actionHistory, app.updates.redoHistory
        app.updates.undo()
        app.updates.undo()
        assert undo_stack == [] and [a.transaction for a in redo_stack] == ["t2", "t1"]
        assert redo_stack[-1].transaction == "t1", "flat actions, like UpdateManager"
        app.updates.redo()
        assert _clip(app.project, "A")["position"] == 10.0 and len(undo_stack) == 1


def test_a_draft_cannot_be_saved_over_or_replaced_by_another_project(world, tmp_path):
    app, store, _, _ = world
    branch = tb.start(store)
    with tb.bound(branch):
        with pytest.raises(RuntimeError):
            app.project.save(str(tmp_path / "x.zvn"))
        with pytest.raises(RuntimeError):
            app.project.load(str(tmp_path / "x.zvn"))


def test_a_waveform_cache_write_is_not_a_conflict(world):
    app, store, _, _ = world
    branch = tb.start(store)
    with tb.bound(branch):
        app.updates.update(["clips", {"id": "A"}], {"position": 10.0})
    app.updates.update_untracked(["clips", {"id": "A"}], {"ui": {"audio_data": [0.1, 0.2]}})
    assert tb.apply_active(app.updates) == []
    assert _clip(store, "A")["position"] == 10.0


def test_a_new_run_takes_a_fresh_copy_when_the_pending_branch_is_empty(world):
    app, store, _, _ = world
    stale = tb.start(store)
    app.updates.update(["clips", {"id": "A"}], {"position": 3.0})  # the user, between turns
    fresh = tb.start(store)
    assert fresh is not stale and _clip(fresh.store, "A")["position"] == 3.0


def test_an_empty_branch_another_run_is_using_is_kept(world):
    _, store, _, _ = world
    in_use = tb.start(store)
    assert tb.start(store, refresh_empty=False) is in_use
