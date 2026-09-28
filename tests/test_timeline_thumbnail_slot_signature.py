"""The native timeline's thumbnail_ready signal and its forwarding slot must agree.

PyQt refuses to connect a ``@pyqtSlot``-decorated method whose declared argument
types do not match the signal, and TimelineWidgetBase.__init__ makes that connect
while the main window is being built, so a mismatch aborts every launch.  The
declarations live in two files, so check them statically (no Qt needed).
"""

import ast
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
QWIDGET_DIR = os.path.join(SRC, "windows", "views", "timeline_backend", "qwidget")


def _call_arg_names(call):
    return [ast.unparse(a) for a in call.args]


def _signal_signatures(path, attr):
    """Return every ``attr = pyqtSignal(...)`` declaration's argument list."""
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if any(isinstance(t, ast.Name) and t.id == attr for t in node.targets):
            found.append(_call_arg_names(node.value))
    return found


def _slot_signature(path, method):
    """Return the ``@pyqtSlot(...)`` argument list decorating ``method``."""
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and ast.unparse(dec.func).endswith("pyqtSlot"):
                    return _call_arg_names(dec)
    raise AssertionError("%s has no @pyqtSlot decorator in %s" % (method, path))


def test_thumbnail_ready_slot_matches_signal():
    signals = _signal_signatures(os.path.join(QWIDGET_DIR, "thumbnails.py"), "thumbnail_ready")
    assert signals, "thumbnail_ready signal declaration not found"
    slot = _slot_signature(os.path.join(QWIDGET_DIR, "base.py"), "_handle_thumbnail_ready")
    for sig in signals:
        assert slot == sig, "slot %s does not match thumbnail_ready signal %s" % (slot, sig)


def test_thumbnail_ready_third_argument_is_object():
    # The manager emits a QImage (or a legacy path string); only ``object`` accepts both.
    slot = _slot_signature(os.path.join(QWIDGET_DIR, "base.py"), "_handle_thumbnail_ready")
    assert slot[2] == "object"
