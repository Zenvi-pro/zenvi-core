"""One tool call must be exactly one undo step.

These tests drive the *real* ``UpdateManager`` behind the *real*
``UpdatesRouter``, because the bug this suite exists to prevent was invisible
to a fake: ``tests/test_tool_handlers_undo.py`` injected an ``app.updates``
whose ``transaction_id`` was a plain attribute, so grouping "worked" there
while in the running app every write to it was swallowed by the router and
every mutation became its own undo step.

The scenario from the field: placing a motion graphic mutates the project
four times (ripple, metadata stamp, clip insert, trim). Undo reversed the
trailing update, reported success, and left the clip on the timeline.
"""

import contextlib
import threading
from unittest.mock import MagicMock, patch

from classes import tool_handlers
from classes import updates as updates_module
from classes.update_queue import UpdateQueue, UpdatesRouter
from classes.updates import UpdateManager


class _Store:
    """A minimal project store: enough for undo to actually reverse things.

    ``UpdateManager.get_reverse_action`` can only invert an action whose
    ``old_values`` were filled in by a listener, which is what the real
    ProjectDataStore does in ``changed()``. This does the same for a flat
    list of clips.
    """

    def __init__(self, clips=None, layers=None):
        self.clips = list(clips or [])
        self.layers = list(layers or [{"number": 1000000, "id": "t1"}])

    # -- listener contract --------------------------------------------------
    def changed(self, action):
        if action.type == "load":
            return
        key = action.key or []
        if not key or key[0] != "clips":
            return
        if action.type == "insert":
            action.set_old_values(None)
            self.clips.append(dict(action.values or {}))
        elif action.type == "update":
            target = self._find(key)
            if target is not None:
                action.set_old_values(dict(target))
                target.update(action.values or {})
        elif action.type == "delete":
            target = self._find(key)
            if target is not None:
                action.set_old_values(dict(target))
                self.clips.remove(target)

    def _find(self, key):
        for part in key:
            if isinstance(part, dict) and "id" in part:
                cid = part["id"]
                return next((c for c in self.clips if c.get("id") == cid), None)
        return None

    # -- app.project.get contract ------------------------------------------
    def get(self, name, default=None):
        if name == "clips":
            return self.clips
        if name == "layers":
            return self.layers
        return default


def _app(clips=None):
    manager = UpdateManager()
    store = _Store(clips)
    manager.add_listener(store)
    app = MagicMock()
    app.project = store
    app.updates = UpdatesRouter(manager, UpdateQueue(manager))
    return app, manager, store


@contextlib.contextmanager
def _driving(app):
    """Point both the handler layer and UpdateManager at *app*.

    UpdateManager.undo() reaches for ``get_app().window`` itself (to drop
    selections on clips it is about to delete), so patching only
    tool_handlers._get_app leaves undo talking to the real application.
    """
    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(updates_module, "get_app", return_value=app):
            yield app


def _ids(store):
    return sorted(c["id"] for c in store.clips)


def _transactions(manager):
    return {a.transaction for a in manager.actionHistory}


# --------------------------------------------------------------------------
# execute_tool groups a whole tool call
# --------------------------------------------------------------------------

def _register(name, fn):
    """Temporarily expose *fn* as a tool named *name*."""
    return patch.dict(tool_handlers.TOOL_HANDLERS, {name: fn})


def test_a_multi_mutation_tool_call_is_one_undo_step():
    """The place_motion_graphic shape: ripple, stamp, insert, trim."""
    app, manager, store = _app(clips=[{"id": "existing", "position": 0.0}])

    def _place(**_kw):
        app.updates.update(["clips", {"id": "existing"}], {"position": 5.0})
        app.updates.insert(["clips"], {"id": "mg", "position": 1.0, "layer": 5000000})
        app.updates.update(["clips", {"id": "mg"}], {"end": 4.8})
        return "placed"

    with _driving(app), \
         _register("place_motion_graphic_tool", _place):
        assert tool_handlers.execute_tool("place_motion_graphic_tool", {}) == "placed"

    assert len(manager.actionHistory) == 3
    assert len(_transactions(manager)) == 1, (
        "three mutations for one tool call must share one transaction id"
    )
    assert "mg" in _ids(store)


def test_one_undo_reverses_the_whole_tool_call():
    """The exact failure from the log: undo must remove the placed clip."""
    app, manager, store = _app(clips=[{"id": "existing", "position": 0.0}])

    def _place(**_kw):
        app.updates.update(["clips", {"id": "existing"}], {"position": 5.0})
        app.updates.insert(["clips"], {"id": "mg", "position": 1.0})
        app.updates.update(["clips", {"id": "mg"}], {"end": 4.8})
        return "placed"

    with _driving(app), \
         _register("place_motion_graphic_tool", _place):
        tool_handlers.execute_tool("place_motion_graphic_tool", {})
        assert _ids(store) == ["existing", "mg"]
        out = tool_handlers.execute_tool("undo_tool", {})

    assert not out.startswith("Error:"), out
    assert _ids(store) == ["existing"], "one undo must remove the placed clip"
    assert manager.actionHistory == []


def test_undo_restores_the_rippled_position_too():
    """Half-undo is the bug: the clip goes but the ripple stays."""
    app, manager, store = _app(clips=[{"id": "existing", "position": 0.0}])

    def _place(**_kw):
        app.updates.update(["clips", {"id": "existing"}], {"position": 5.0})
        app.updates.insert(["clips"], {"id": "mg", "position": 1.0})
        return "placed"

    with _driving(app), \
         _register("place_motion_graphic_tool", _place):
        tool_handlers.execute_tool("place_motion_graphic_tool", {})
        assert store.clips[0]["position"] == 5.0
        tool_handlers.execute_tool("undo_tool", {})

    assert _ids(store) == ["existing"]
    assert store.clips[0]["position"] == 0.0, "ripple was left behind"


def test_two_tool_calls_are_two_undo_steps():
    """Grouping must not glue separate calls together."""
    app, manager, _store = _app()

    def _add_a(**_kw):
        app.updates.insert(["clips"], {"id": "a"})
        return "a"

    def _add_b(**_kw):
        app.updates.insert(["clips"], {"id": "b"})
        return "b"

    with _driving(app), \
         _register("add_clip_to_timeline_tool", _add_a):
        tool_handlers.execute_tool("add_clip_to_timeline_tool", {})
    with _driving(app), \
         _register("add_clip_to_timeline_tool", _add_b):
        tool_handlers.execute_tool("add_clip_to_timeline_tool", {})

    assert len(_transactions(manager)) == 2


def test_a_handler_opening_its_own_transaction_joins_the_call_group():
    """_atomic inside a handler must not split off a second undo step."""
    app, manager, _store = _app()

    def _composite(**_kw):
        with tool_handlers._transaction(app):
            app.updates.insert(["clips"], {"id": "a"})
        with tool_handlers._transaction(app):
            app.updates.insert(["clips"], {"id": "b"})
        return "ok"

    with _driving(app), \
         _register("generate_video_and_add_to_timeline_tool", _composite):
        tool_handlers.execute_tool("generate_video_and_add_to_timeline_tool", {})

    assert len(_transactions(manager)) == 1


def test_undo_and_redo_do_not_open_a_transaction():
    """A transaction around undo would stamp the reversal actions with it."""
    assert "undo_tool" in tool_handlers._UNGROUPED_TOOLS
    assert "redo_tool" in tool_handlers._UNGROUPED_TOOLS
    assert tool_handlers.READ_ONLY_TOOLS <= tool_handlers._UNGROUPED_TOOLS


def test_redo_after_undo_restores_the_whole_group():
    app, manager, store = _app()

    def _place(**_kw):
        app.updates.insert(["clips"], {"id": "mg", "position": 1.0})
        app.updates.update(["clips", {"id": "mg"}], {"end": 4.8})
        return "placed"

    with _driving(app), \
         _register("place_motion_graphic_tool", _place):
        tool_handlers.execute_tool("place_motion_graphic_tool", {})
        tool_handlers.execute_tool("undo_tool", {})
        assert _ids(store) == []
        tool_handlers.execute_tool("redo_tool", {})

    assert _ids(store) == ["mg"]


# --------------------------------------------------------------------------
# Honest reporting
# --------------------------------------------------------------------------

def test_undo_names_what_left_the_timeline():
    app, _manager, _store = _app()

    def _place(**_kw):
        app.updates.insert(["clips"], {"id": "Q0VNEWBAZT", "position": 0.96})
        return "placed"

    with _driving(app), \
         _register("place_motion_graphic_tool", _place):
        tool_handlers.execute_tool("place_motion_graphic_tool", {})
        out = tool_handlers.execute_tool("undo_tool", {})

    assert "Q0VNEWBAZT" in out, out
    assert "removed" in out, out


def test_undo_reports_an_error_when_the_timeline_did_not_change():
    """The reported failure, reduced: a history step pops, nothing moves.

    Reported as an error rather than "Undid 1 action", so the agent does not
    tell the user the change was reversed when it was not.
    """
    app, manager, store = _app(clips=[{"id": "stuck", "position": 0.0}])

    # A clips-keyed action whose reversal the store cannot apply (no such
    # clip), so the stack shrinks while the timeline stays put.
    app.updates.transaction_id = "T1"
    app.updates.update(["clips", {"id": "ghost"}], {"position": 9.0})
    app.updates.transaction_id = None
    assert len(manager.actionHistory) == 1

    with _driving(app):
        out = tool_handlers.execute_tool("undo_tool", {})

    assert out.startswith("Error:"), out
    assert "did not change" in out
    assert _ids(store) == ["stuck"]


def test_undo_of_a_non_clip_action_is_not_reported_as_a_failure():
    """Markers, export settings and track renames move no clips, legitimately."""
    app, manager, _store = _app(clips=[{"id": "keep", "position": 0.0}])

    app.updates.transaction_id = "T1"
    app.updates.update(["export"], {"width": 1920})
    app.updates.transaction_id = None

    with _driving(app):
        out = tool_handlers.execute_tool("undo_tool", {})

    assert not out.startswith("Error:"), out
    assert "Undid 1 action" in out


# --------------------------------------------------------------------------
# Thread scoping
# --------------------------------------------------------------------------

def test_transaction_id_does_not_leak_between_threads():
    """Concurrent tool calls must not merge into one undo step.

    The prompt tells the agent to fire independent timeline edits in parallel,
    and each arrives on its own worker thread.
    """
    manager = UpdateManager()
    manager.transaction_id = "MAIN"
    seen = []

    def _worker():
        seen.append(manager.transaction_id)
        manager.transaction_id = "WORKER"
        seen.append(manager.transaction_id)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    assert seen == [None, "WORKER"], seen
    assert manager.transaction_id == "MAIN", "worker clobbered the main thread"


def test_ignore_history_does_not_leak_between_threads():
    manager = UpdateManager()
    manager.ignore_history = True
    seen = []

    t = threading.Thread(target=lambda: seen.append(manager.ignore_history))
    t.start()
    t.join()

    assert seen == [False]
    assert manager.ignore_history is True


def test_two_parallel_tool_calls_stay_separate_undo_steps():
    app, manager, _store = _app()
    barrier = threading.Barrier(2)

    def _make(cid):
        def _tool(**_kw):
            barrier.wait(timeout=5)   # force the two calls to interleave
            app.updates.insert(["clips"], {"id": cid})
            return cid
        return _tool

    def _run(cid):
        with _driving(app), \
             _register(f"tool_{cid}", _make(cid)):
            tool_handlers.execute_tool(f"tool_{cid}", {})

    threads = [threading.Thread(target=_run, args=(c,)) for c in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(manager.actionHistory) == 2
    assert len(_transactions(manager)) == 2, (
        "two independent tool calls merged into a single undo step"
    )


# --------------------------------------------------------------------------
# Contiguous-tail grouping
# --------------------------------------------------------------------------

def test_grouping_survives_an_interleaved_operation():
    """Two composites interleaving their hops must still undo as wholes.

    A background-safe tool does its network work off the Qt thread and
    marshals each mutation over separately, so two concurrent operations
    write history as A, B, A. Grouping only the contiguous tail would undo
    that last A and leave the first applied -- a half-undone edit, which is
    the failure this module exists to prevent.
    """
    manager = UpdateManager()
    manager.add_listener(_Store())

    manager.transaction_id = "A"
    manager.insert(["clips"], {"id": "a1"})
    manager.transaction_id = "B"
    manager.insert(["clips"], {"id": "b1"})
    manager.transaction_id = "A"
    manager.insert(["clips"], {"id": "a2"})
    manager.transaction_id = None

    group = manager._tail_transaction(manager.actionHistory)
    assert [a.values["id"] for a in group] == ["a2", "a1"], (
        "operation A was split across the interleaving B and only half undone"
    )


def test_grouping_leaves_the_interleaved_operation_alone():
    """Undoing A must not touch B."""
    manager = UpdateManager()
    store = _Store()
    manager.add_listener(store)

    manager.transaction_id = "A"
    manager.insert(["clips"], {"id": "a1"})
    manager.transaction_id = "B"
    manager.insert(["clips"], {"id": "b1"})
    manager.transaction_id = "A"
    manager.insert(["clips"], {"id": "a2"})
    manager.transaction_id = None

    group = manager._tail_transaction(manager.actionHistory)
    assert "b1" not in [a.values["id"] for a in group]


def test_tail_transaction_of_an_empty_history_is_empty():
    assert UpdateManager._tail_transaction([]) == []


def test_tail_transaction_is_most_recent_first():
    manager = UpdateManager()
    manager.add_listener(_Store())
    manager.transaction_id = "T"
    manager.insert(["clips"], {"id": "first"})
    manager.insert(["clips"], {"id": "second"})
    manager.transaction_id = None

    group = manager._tail_transaction(manager.actionHistory)
    assert [a.values["id"] for a in group] == ["second", "first"]


# --------------------------------------------------------------------------
# Coverage: every mutating tool is grouped, by default
# --------------------------------------------------------------------------
#
# The grouping is structural -- execute_tool wraps every tool that is not
# explicitly exempt -- so this asserts the *exemption list* rather than
# executing 60 handlers (most of which need network, ffmpeg or a render farm).
# A newly added mutating tool is covered the moment it is registered; a new
# exemption has to be added here, deliberately, with a reason.

_EXPECTED_UNGROUPED = {
    # Walk the history stack; a transaction around them would stamp the
    # caller's id onto the reversal actions and glue separate steps together.
    "undo_tool",
    "redo_tool",
}


def test_only_read_only_and_history_tools_are_ungrouped():
    unexpected = (
        tool_handlers._UNGROUPED_TOOLS
        - tool_handlers.READ_ONLY_TOOLS
        - _EXPECTED_UNGROUPED
    )
    assert unexpected == set(), (
        f"these tools mutate the project but are not grouped into one undo "
        f"step: {sorted(unexpected)}"
    )


def test_every_registered_mutating_tool_is_grouped():
    mutating = set(tool_handlers.AGENT_TOOL_HANDLERS) - tool_handlers._UNGROUPED_TOOLS
    assert mutating, "sanity: there should be mutating tools"
    # Nothing to assert per-tool -- being outside _UNGROUPED_TOOLS *is* the
    # guarantee, since execute_tool wraps everything else. This pins that the
    # set is derived, not hand-maintained per tool.
    assert tool_handlers._UNGROUPED_TOOLS >= tool_handlers.READ_ONLY_TOOLS


def test_background_safe_tools_are_still_grouped():
    """They run off the Qt main thread and mutate across several hops.

    Those hops are exactly the case _run_on_main_thread's id propagation
    exists for, so they must not be exempted.
    """
    leaked = tool_handlers.BACKGROUND_SAFE_TOOLS & tool_handlers._UNGROUPED_TOOLS
    assert leaked <= tool_handlers.READ_ONLY_TOOLS, (
        f"background-safe mutating tools must still be grouped: {sorted(leaked)}"
    )


def test_delete_tools_are_grouped():
    for name in ("delete_from_timeline_tool", "remove_clip_tool",
                 "delete_clips_on_track_tool"):
        assert name not in tool_handlers._UNGROUPED_TOOLS


def test_a_track_clear_is_a_single_undo_step():
    """Fifty deletions, one undo -- the claim the result string now makes."""
    clips = [{"id": f"c{i}", "layer": 1000000, "position": float(i)} for i in range(50)]
    app, manager, store = _app(clips=list(clips))

    def _clear(**_kw):
        for c in list(store.clips):
            app.updates.delete(["clips", {"id": c["id"]}])
        return "cleared"

    with _driving(app), _register("delete_from_timeline_tool", _clear):
        tool_handlers.execute_tool("delete_from_timeline_tool", {})
        assert store.clips == []
        tool_handlers.execute_tool("undo_tool", {})

    assert len(_transactions(manager)) == 0
    assert len(store.clips) == 50, "one undo must restore the whole track"
