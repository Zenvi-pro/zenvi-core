"""A traceback in a QWebChannel bridge slot must not wedge the calling panel.

An exception raised inside a ``@pyqtSlot`` that JavaScript invoked over
QWebChannel leaves the JS-side promise unresolved -- the panel then stops
responding with no error and no spinner.  ``classes.bridge_guard.guarded_slot``
replaces ``pyqtSlot`` on the bridge classes so the traceback is logged and a
value of the *declared* return type goes back to JS.

It has to replace the decorator rather than wrap the class afterwards: Qt
dispatches through the meta-object, so re-registering a slot on a subclass
leaves two entries of one signature and QtWebKit rejects the call as an
ambiguous overload (``ScrollbarChanged()`` from ruler.js).  That is what
``test_each_slot_is_registered_exactly_once`` pins.

The interesting assertions go through ``QMetaObject.invokeMethod`` rather than
a plain Python call, which needs real Qt: run with ``ZENVI_REAL_QT=1``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PyQt5.QtCore import QObject  # noqa: E402

from classes import bridge_guard  # noqa: E402

# conftest installs a headless PyQt5 stub by default (QObject is object, and
# pyqtSlot is the identity function).  There is no meta-object to dispatch
# through then, so only the plain-Python behaviour is checkable.
REAL_QT = QObject is not object
requires_qt = pytest.mark.skipif(not REAL_QT, reason="needs ZENVI_REAL_QT=1")


@pytest.fixture
def reports(monkeypatch):
    """Collect what the guard hands to crash_handler.report()."""
    calls = []

    def fake_report(exc_type, exc_value, exc_tb, context="", show_dialog=True, **kw):
        calls.append({"exc": exc_value, "context": context, "show_dialog": show_dialog})

    monkeypatch.setattr(bridge_guard.crash_handler, "report", fake_report)
    return calls


@pytest.fixture
def bridge():
    """A bridge class covering every return shape JS can ask a slot for."""
    guarded_slot = bridge_guard.guarded_slot

    class Bridge(QObject):
        @guarded_slot(str, result=int)
        def failing_int(self, value):
            raise RuntimeError("int boom")

        @guarded_slot(result=str)
        def failing_str(self):
            raise RuntimeError("str boom")

        @guarded_slot(result=bool)
        def failing_bool(self):
            raise RuntimeError("bool boom")

        @guarded_slot(result="QVariantList")
        def failing_list(self):
            raise RuntimeError("list boom")

        @guarded_slot(str)
        def failing_void(self, value):
            raise RuntimeError("void boom")

        @guarded_slot(str, result=str)
        def working(self, value):
            return value.upper()

        @guarded_slot(list)
        def ScrollbarChanged(self, positions):
            self.seen = positions

    return Bridge


def _invoke(obj, name, result_type=None, *args):
    from PyQt5.QtCore import QMetaObject, Q_ARG, Q_RETURN_ARG

    qargs = [Q_ARG(type(a), a) for a in args]
    if result_type is None:
        return QMetaObject.invokeMethod(obj, name, *qargs)
    return QMetaObject.invokeMethod(obj, name, Q_RETURN_ARG(result_type), *qargs)


@requires_qt
@pytest.mark.parametrize("slot, result_type, expected", [
    ("failing_int", int, 0),
    ("failing_str", str, ""),
    ("failing_bool", bool, False),
    ("failing_list", list, []),
])
def test_raising_slot_returns_declared_default_over_the_metaobject(
        bridge, reports, slot, result_type, expected):
    """The JS promise resolves with a typed value instead of hanging forever."""
    obj = bridge()
    args = ("x",) if slot == "failing_int" else ()
    assert _invoke(obj, slot, result_type, *args) == expected
    assert len(reports) == 1


@requires_qt
def test_void_slot_swallows_and_reports_without_a_dialog(bridge, reports):
    obj = bridge()
    _invoke(obj, "failing_void", None, "x")
    assert len(reports) == 1
    assert reports[0]["show_dialog"] is False
    assert "failing_void" in reports[0]["context"]
    assert str(reports[0]["exc"]) == "void boom"


@requires_qt
def test_working_slot_is_untouched(bridge, reports):
    """Guarding must not change the value a healthy slot returns."""
    obj = bridge()
    assert _invoke(obj, "working", str, "abc") == "ABC"
    assert reports == []


@requires_qt
def test_each_slot_is_registered_exactly_once(bridge):
    """Two entries of one signature make QtWebKit reject the call as ambiguous."""
    from PyQt5.QtCore import QMetaMethod

    meta = bridge.staticMetaObject
    registered = [
        bytes(meta.method(i).methodSignature())
        for i in range(QObject.staticMetaObject.methodCount(), meta.methodCount())
        if meta.method(i).methodType() == QMetaMethod.Slot
    ]
    assert len(registered) == len(set(registered)), registered
    assert b"ScrollbarChanged(QVariantList)" in registered


def test_guard_catches_on_a_plain_python_call(bridge, reports):
    """Slots are called from Python too; the guard is not Qt-dispatch-only."""
    obj = bridge()
    assert obj.failing_int("x") == 0
    assert obj.working("abc") == "ABC"
    assert len(reports) == 1


def test_healthy_slot_still_runs_its_body(bridge, reports):
    obj = bridge()
    obj.ScrollbarChanged([1, 2])
    assert obj.seen == [1, 2]
    assert reports == []


def test_real_bridges_use_guarded_slots():
    """The registered QWebChannel bridges actually opt in."""
    from windows.plan_dock_ui import PlanDockBridge
    from windows.ai_chat_ui import ChatBridge

    def guarded(cls):
        return sorted(name for name, value in vars(cls).items()
                      if getattr(value, "__zenvi_guarded__", False))

    assert guarded(PlanDockBridge) == ["editPlanInPlanningMode", "executePlan"]
    assert "submitPlanAnswers" in guarded(ChatBridge)
    assert "cancelRequest" in guarded(ChatBridge)


# ── every slot on every bridge, without importing the GUI modules ────────
#
# TimelineView pulls in libopenshot and a real QWebEngine/QtWebKit view, so it
# cannot be imported headlessly.  Reading the source keeps the "no bare
# pyqtSlot survives on a bridge class" check honest for all three classes
# instead of only the two that import.

BRIDGE_CLASSES = [
    ("windows/plan_dock_ui.py", "PlanDockBridge"),
    ("windows/ai_chat_ui.py", "ChatBridge"),
    ("windows/views/timeline.py", "TimelineView"),
]


def _slot_decorated_methods(rel_path, class_name):
    """(method name, decorator names) for every slot-decorated method."""
    import ast

    tree = ast.parse((SRC / rel_path).read_text(encoding="utf-8"))
    cls = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    assert cls is not None, f"{class_name} not found in {rel_path}"

    found = []
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = []
        for dec in node.decorator_list:
            call = dec.func if isinstance(dec, ast.Call) else dec
            names.append(call.attr if isinstance(call, ast.Attribute) else getattr(call, "id", ""))
        if {"pyqtSlot", "guarded_slot"} & set(names):
            found.append((node.name, names))
    return found


@pytest.mark.parametrize("rel_path, class_name", BRIDGE_CLASSES)
def test_every_bridge_slot_is_guarded(rel_path, class_name):
    """A bare @pyqtSlot on a bridge is the hang this PR fixes."""
    slots = _slot_decorated_methods(rel_path, class_name)
    assert slots, f"no slots found on {class_name}"
    unguarded = [name for name, decs in slots if "guarded_slot" not in decs]
    assert unguarded == [], f"{class_name} slots still using bare pyqtSlot: {unguarded}"


# ── a guarded slot must not leave shared state half-mutated ─────────────


def test_transaction_is_cleared_when_the_slot_body_raises():
    """A dead transaction id groups every later edit into one undo step."""
    updates = type("U", (), {"transaction_id": None, "ignore_history": False})()

    with pytest.raises(RuntimeError):
        with bridge_guard.slot_transaction(updates, "tx-1"):
            assert updates.transaction_id == "tx-1"
            raise RuntimeError("save failed")

    assert updates.transaction_id is None


def test_transaction_is_cleared_on_success():
    updates = type("U", (), {"transaction_id": None})()
    with bridge_guard.slot_transaction(updates, "tx-1"):
        pass
    assert updates.transaction_id is None


def test_no_transaction_id_leaves_an_outer_transaction_alone():
    """JS calls without a transaction id must not clobber an in-flight one."""
    updates = type("U", (), {"transaction_id": "outer"})()
    with bridge_guard.slot_transaction(updates, None):
        pass
    assert updates.transaction_id == "outer"


def test_submit_plan_answers_restores_session_state_when_dispatch_fails():
    """Otherwise Skip/Submit silently stops working for the rest of the session."""
    from windows.ai_chat_ui import ChatBridge

    sess = {"awaiting_plan_answers": True, "pending_plan_questions": [{"id": "q1"}]}

    class FakeWindow:
        is_processing = True
        model_combo = None
        processing_calls = []

        def _active_session(self):
            return sess

        def _set_processing_ui(self, value):
            self.processing_calls.append(value)
            self.is_processing = value

        def _dispatch_user_message(self, *a, **kw):
            raise RuntimeError("websocket down")

    bridge = ChatBridge.__new__(ChatBridge)
    bridge.window = FakeWindow()
    bridge.submitPlanAnswers('{"skip": true}')

    assert sess["awaiting_plan_answers"] is True
    assert sess["pending_plan_questions"] == [{"id": "q1"}]
    assert bridge.window.is_processing is True
