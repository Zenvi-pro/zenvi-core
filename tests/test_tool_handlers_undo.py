"""Undo/redo tool handler tests.

Covers the failure modes the agent hit in practice:
  * "Undo performed." reported on an empty history stack (false success)
  * the preview never refreshing because the handler skipped refreshFrameSignal
  * one user-facing action (place + trim) landing as two undo steps
"""

import contextlib
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub Qt before importing tool_handlers (headless CI).
# QThread = None is what makes execute_tool/_run_on_main_thread call through
# directly instead of marshalling to a Qt main thread that does not exist here.
_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

from classes import tool_handlers  # noqa: E402


class _Action:
    """Minimal stand-in for classes.updates.UpdateAction."""

    def __init__(self, type_, key, transaction):
        self.type = type_
        self.key = key
        self.transaction = transaction


class _FakeUpdates:
    """A history stack with UpdateManager's observable contract.

    Notably: undo()/redo() return None and silently no-op on an empty stack,
    and they move a whole transaction group at a time.
    """

    def __init__(self, action_history=None, redo_history=None):
        self.actionHistory = list(action_history or [])
        self.redoHistory = list(redo_history or [])
        self.transaction_id = None
        self.ignore_history = False

    def _pop_group(self, src, dst):
        if not src:
            return  # silent no-op, exactly like UpdateManager
        tid = src[-1].transaction
        for a in [x for x in reversed(src) if x.transaction == tid]:
            src.remove(a)
            dst.append(a)

    def undo(self):
        self._pop_group(self.actionHistory, self.redoHistory)

    def redo(self):
        self._pop_group(self.redoHistory, self.actionHistory)


def _app(updates):
    app = MagicMock()
    app.updates = updates
    return app


@contextlib.contextmanager
def _patched(app):
    """Point tool_handlers at *app* and force the no-Qt code path.

    The repo has no conftest.py, so the PyQt5 stub above is installed with
    sys.modules.setdefault() — first module to run wins.  When another test
    file imports tool_handlers first, tool_handlers.QThread is that file's
    MagicMock rather than None, and _run_on_main_thread tries to marshal to a
    Qt main thread that does not exist.  Patch it per-test so these tests do
    not depend on collection order.
    """
    with patch.object(tool_handlers, "_get_app", return_value=app),             patch.object(tool_handlers, "QThread", None):
        yield app


_PROJECT = {
    "fps": {"num": 30, "den": 1},
    "layers": [{"number": 0, "id": "L0"}, {"number": 1, "id": "L1"}],
    "clips": [],
}


def _history(*groups):
    """_history(("insert", 2), ("update", 1)) -> oldest transaction first."""
    out = []
    for i, (kind, count) in enumerate(groups):
        for _ in range(count):
            out.append(_Action(kind, ["clips"], "txn-%d" % i))
    return out


# --------------------------------------------------------------------------
# Honest empty-stack reporting
# --------------------------------------------------------------------------

def test_undo_on_empty_history_is_an_error():
    updates = _FakeUpdates()
    with _patched(_app(updates)):
        out = tool_handlers.undo()
    assert out.startswith("Error:"), out
    assert "nothing to undo" in out
    assert updates.actionHistory == []


def test_redo_on_empty_history_is_an_error():
    updates = _FakeUpdates()
    with _patched(_app(updates)):
        out = tool_handlers.redo()
    assert out.startswith("Error:"), out
    assert "nothing to redo" in out


def test_empty_undo_is_not_reported_as_success():
    """ai_chat_ui treats any non-'Error' string as success, so this matters."""
    updates = _FakeUpdates()
    with _patched(_app(updates)):
        out = tool_handlers.undo()
    assert "Undo performed" not in out


# --------------------------------------------------------------------------
# Single-step undo / redo
# --------------------------------------------------------------------------

def test_single_undo_pops_one_transaction_and_describes_it():
    updates = _FakeUpdates(_history(("insert", 1), ("insert", 2)))
    with _patched(_app(updates)):
        out = tool_handlers.undo()
    assert "Undid 1 action" in out, out
    assert "insert x2" in out, out
    assert "on clips" in out, out
    assert len(updates.actionHistory) == 1
    assert len(updates.redoHistory) == 2


def test_redo_restores_what_undo_removed():
    updates = _FakeUpdates(_history(("insert", 2)))
    app = _app(updates)
    with _patched(app):
        tool_handlers.undo()
        assert updates.actionHistory == []
        out = tool_handlers.redo()
    assert "Redid 1 action" in out, out
    assert len(updates.actionHistory) == 2
    assert updates.redoHistory == []


def test_remaining_steps_are_reported():
    updates = _FakeUpdates(_history(("insert", 1), ("update", 1), ("delete", 1)))
    with _patched(_app(updates)):
        out = tool_handlers.undo()
    assert "2 undo steps remain." in out, out


def test_singular_remaining_step_reads_correctly():
    updates = _FakeUpdates(_history(("insert", 1), ("update", 1)))
    with _patched(_app(updates)):
        out = tool_handlers.undo()
    assert "1 undo step remains." in out, out


def test_last_undo_says_nothing_left():
    updates = _FakeUpdates(_history(("insert", 1)))
    with _patched(_app(updates)):
        out = tool_handlers.undo()
    assert "Nothing left to undo" in out, out


# --------------------------------------------------------------------------
# steps=N
# --------------------------------------------------------------------------

def test_steps_three_undoes_three_transactions_in_order():
    updates = _FakeUpdates(_history(("insert", 1), ("update", 1), ("delete", 1)))
    with _patched(_app(updates)):
        out = tool_handlers.undo(steps=3)
    assert "Undid 3 actions" in out, out
    # Multi-step must not name one group as if it described them all.
    assert "(" not in out, out
    assert updates.actionHistory == []
    # Reversed most-recent-first: delete, then update, then insert.
    assert [a.type for a in updates.redoHistory] == ["delete", "update", "insert"]


def test_steps_beyond_history_reports_partial():
    updates = _FakeUpdates(_history(("insert", 1), ("update", 1)))
    with _patched(_app(updates)):
        out = tool_handlers.undo(steps=5)
    assert "Undid 2 of 5 requested" in out, out
    assert "nothing left to undo" in out, out
    assert updates.actionHistory == []


def test_steps_is_capped():
    updates = _FakeUpdates(_history(*[("insert", 1)] * 25))
    with _patched(_app(updates)):
        tool_handlers.undo(steps=999)
    assert len(updates.actionHistory) == 25 - tool_handlers._MAX_UNDO_STEPS


def test_coerce_steps_tolerates_llm_junk():
    c = tool_handlers._coerce_steps
    assert c(1) == 1
    assert c(3) == 3
    assert c("3") == 3
    assert c("three") == 3
    assert c("twice") == 2
    assert c(None) == 1
    assert c("") == 1
    assert c(0) == 1
    assert c(-4) == 1
    assert c("banana") == 1
    assert c(999) == tool_handlers._MAX_UNDO_STEPS
    assert c(2.0) == 2


# --------------------------------------------------------------------------
# Preview refresh (the one step the GUI path does and the agent path did not)
# --------------------------------------------------------------------------

def test_refresh_emitted_once_per_call():
    updates = _FakeUpdates(_history(("insert", 1), ("update", 1), ("delete", 1)))
    app = _app(updates)
    with _patched(app):
        tool_handlers.undo(steps=3)
    assert app.window.refreshFrameSignal.emit.call_count == 1


def test_refresh_failure_does_not_break_the_handler():
    updates = _FakeUpdates(_history(("insert", 1)))
    app = _app(updates)
    app.window.refreshFrameSignal.emit.side_effect = RuntimeError("no window")
    with _patched(app):
        out = tool_handlers.undo()
    assert out.startswith("Undid 1 action"), out


# --------------------------------------------------------------------------
# Transaction grouping
# --------------------------------------------------------------------------

def test_transaction_groups_mutations_into_one_undo_step():
    """Place + trim under one transaction id -> a single undo reverses both."""
    updates = _FakeUpdates()
    app = _app(updates)

    with tool_handlers._transaction(app) as tid:
        updates.actionHistory.append(_Action("insert", ["clips"], tid))
        updates.actionHistory.append(_Action("update", ["clips"], tid))

    with _patched(app):
        out = tool_handlers.undo()
    assert updates.actionHistory == [], "one undo must reverse the whole action"
    assert "Undid 1 action" in out, out


def test_ungrouped_mutations_need_two_undos():
    """Regression: the pre-fix behaviour that grouping exists to remove."""
    updates = _FakeUpdates()
    updates.actionHistory.append(_Action("insert", ["clips"], "auto-uuid-1"))
    updates.actionHistory.append(_Action("update", ["clips"], "auto-uuid-2"))
    app = _app(updates)
    with _patched(app):
        tool_handlers.undo()
    assert len(updates.actionHistory) == 1, "trim reverted, clip still there"


def test_transaction_clears_on_exception():
    updates = _FakeUpdates()
    app = _app(updates)
    try:
        with tool_handlers._transaction(app):
            raise ValueError("boom")
    except ValueError:
        pass
    assert updates.transaction_id is None, "a leaked tid glues later edits together"


def test_nested_transaction_joins_the_outer_group():
    """A helper opening its own transaction must not split off a second step."""
    updates = _FakeUpdates()
    app = _app(updates)
    with tool_handlers._transaction(app) as outer:
        with tool_handlers._transaction(app) as inner:
            assert inner == outer, "inner block must join, not nest"
            assert updates.transaction_id == outer
        assert updates.transaction_id == outer, "inner must leave the outer tid"
    assert updates.transaction_id is None


def test_explicit_tid_is_honoured():
    updates = _FakeUpdates()
    app = _app(updates)
    with tool_handlers._transaction(app, tid="shared-1") as tid:
        assert tid == "shared-1"
        assert updates.transaction_id == "shared-1"
    assert updates.transaction_id is None


def test_composite_operation_across_two_hops_is_one_undo_step():
    """Ripple then place: two main-thread hops, one shared id, ONE undo.

    This is the video_gen / motion-graphic cut_in shape — the ripple and the
    placement run in separate _run_on_main_thread hops, so they can only be
    grouped by passing the same transaction id to both.
    """
    updates = _FakeUpdates()
    app = _app(updates)
    tid = tool_handlers._new_transaction_id()

    def _ripple():
        # three downstream clips shifted
        for _ in range(3):
            updates.actionHistory.append(
                _Action("update", ["clips"], updates.transaction_id)
            )

    def _place():
        updates.actionHistory.append(
            _Action("insert", ["clips"], updates.transaction_id)
        )
        updates.actionHistory.append(
            _Action("update", ["clips"], updates.transaction_id)
        )

    tool_handlers._atomic(app, _ripple, tid=tid)()
    tool_handlers._atomic(app, _place, tid=tid)()

    assert len(updates.actionHistory) == 5
    assert {a.transaction for a in updates.actionHistory} == {tid}

    with _patched(app):
        out = tool_handlers.undo()
    assert updates.actionHistory == [], "ripple + placement must undo together"
    assert "Undid 1 action" in out, out


def test_composite_hops_without_a_shared_id_need_two_undos():
    """Regression: the pre-fix shape, where undo only reverted the placement."""
    updates = _FakeUpdates()
    app = _app(updates)

    def _ripple():
        updates.actionHistory.append(
            _Action("update", ["clips"], updates.transaction_id)
        )

    def _place():
        updates.actionHistory.append(
            _Action("insert", ["clips"], updates.transaction_id)
        )

    tool_handlers._atomic(app, _ripple)()   # each mints its own id
    tool_handlers._atomic(app, _place)()

    with _patched(app):
        tool_handlers.undo()
    assert len(updates.actionHistory) == 1, "ripple left behind by one undo"


def test_atomic_without_tid_still_mints_one():
    updates = _FakeUpdates()
    app = _app(updates)
    seen = []
    tool_handlers._atomic(app, lambda: seen.append(updates.transaction_id))()
    assert seen[0] is not None
    assert updates.transaction_id is None


def test_atomic_wrapper_groups_and_returns_value():
    updates = _FakeUpdates()
    app = _app(updates)
    seen = []

    def _mutate():
        seen.append(updates.transaction_id)
        return "done"

    assert tool_handlers._atomic(app, _mutate)() == "done"
    assert seen[0] is not None
    assert updates.transaction_id is None


def test_ignore_history_restored_on_exception():
    updates = _FakeUpdates()
    app = _app(updates)
    try:
        with tool_handlers._ignore_history(app):
            assert updates.ignore_history is True
            raise ValueError("boom")
    except ValueError:
        pass
    assert updates.ignore_history is False, "a leak disables undo globally"


def test_set_export_setting_does_not_leak_ignore_history():
    updates = _FakeUpdates()
    app = _app(updates)
    app.project.get.return_value = {}
    updates.update = MagicMock(side_effect=RuntimeError("store down"))
    with _patched(app):
        out = tool_handlers.set_export_setting(key="width", value="1920")
    assert out.startswith("Error:"), out
    assert updates.ignore_history is False


def test_describe_group_does_not_invent_names():
    out = tool_handlers._describe_group(
        [_Action("insert", ["clips"], "t"), _Action("insert", ["clips"], "t")]
    )
    assert out == "insert x2 on clips", out
    assert tool_handlers._describe_group([]) == ""


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def test_execute_tool_passes_steps_through():
    updates = _FakeUpdates(_history(("insert", 1), ("update", 1)))
    with _patched(_app(updates)):
        out = tool_handlers.execute_tool("undo_tool", {"steps": 2})
    assert "Undid 2 actions" in out, out
    assert updates.actionHistory == []


def test_main_thread_timeout_scales_with_steps():
    assert tool_handlers._main_thread_timeout("play_tool", {}) == 30
    assert tool_handlers._main_thread_timeout("undo_tool", {"steps": 20}) > 30
    assert tool_handlers._main_thread_timeout("undo_tool", {"steps": 1}) == 30


def test_undo_is_not_background_safe():
    """undo/redo must keep marshalling to the Qt main thread."""
    for name in ("undo_tool", "redo_tool"):
        assert name not in tool_handlers.READ_ONLY_TOOLS
        assert name not in tool_handlers.BACKGROUND_SAFE_TOOLS


def test_add_clip_to_timeline_forwards_the_caller_transaction_id():
    """The wiring that makes ripple + placement a single undo step.

    generate_video_and_add_to_timeline and place_motion_graphic ripple the
    timeline in one main-thread hop, then call this handler for the placement
    in another.  The id has to reach _atomic here or they split into two steps.
    """
    updates = _FakeUpdates()
    app = _app(updates)
    app.project.get.side_effect = lambda k, d=None: _PROJECT.get(k, d)

    mock_file = MagicMock()
    mock_file.id = "file-1"
    mock_file.data = {"path": "/tmp/v.mp4", "media_type": "video",
                      "has_video": True, "duration": 10.0}
    query = MagicMock()
    query.File.get.return_value = mock_file
    query.Clip.filter.return_value = []

    seen = {}

    def _spy_atomic(_app, func, tid=None):
        seen["tid"] = tid
        return func

    with patch.dict(sys.modules, {"classes.query": query}),             patch.object(tool_handlers, "_get_app", return_value=app),             patch.object(tool_handlers, "QThread", None),             patch.object(tool_handlers, "_atomic", _spy_atomic),             patch.object(tool_handlers, "_run_on_main_thread",
                         lambda fn, *a, **k: fn(*a)):
        tool_handlers.add_clip_to_timeline(
            file_id="file-1",
            position_seconds="5",
            track="1",
            transaction_id="shared-tid",
        )

    assert seen.get("tid") == "shared-tid", seen


def test_add_clip_to_timeline_mints_its_own_id_when_standalone():
    updates = _FakeUpdates()
    app = _app(updates)
    app.project.get.side_effect = lambda k, d=None: _PROJECT.get(k, d)

    mock_file = MagicMock()
    mock_file.id = "file-1"
    mock_file.data = {"path": "/tmp/v.mp4", "media_type": "video",
                      "has_video": True, "duration": 10.0}
    query = MagicMock()
    query.File.get.return_value = mock_file
    query.Clip.filter.return_value = []

    seen = {}

    def _spy_atomic(_app, func, tid=None):
        seen["tid"] = tid
        return func

    with patch.dict(sys.modules, {"classes.query": query}),             patch.object(tool_handlers, "_get_app", return_value=app),             patch.object(tool_handlers, "QThread", None),             patch.object(tool_handlers, "_atomic", _spy_atomic),             patch.object(tool_handlers, "_run_on_main_thread",
                         lambda fn, *a, **k: fn(*a)):
        tool_handlers.add_clip_to_timeline(
            file_id="file-1", position_seconds="5", track="1",
        )

    assert "tid" in seen, "handler never reached _atomic"
    assert seen["tid"] is None, "standalone placement must mint its own id"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("test_tool_handlers_undo: ok")
