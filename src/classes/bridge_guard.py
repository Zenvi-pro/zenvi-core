"""Keep a traceback in a QWebChannel bridge slot from wedging the calling panel.

A Python exception raised inside a ``@pyqtSlot`` that JavaScript invoked over
QWebChannel leaves the JS-side promise unresolved: the panel that made the call
stops responding, with no error and no spinner (the
``channel.execCallbacks[message.id] is not a function`` console error).  It is a
hang rather than a crash, so the process-wide handlers in ``crash_handler`` do
not cover it.

``guarded_slot`` is a drop-in for ``pyqtSlot`` that registers a wrapped
function: the traceback is logged and a value of the *declared* return type
goes back to JS instead.  It has to replace the decorator rather than wrap the
class afterwards -- Qt dispatches through the meta-object, which is built at
class creation, so an instance attribute is never consulted and re-registering
the slot on a subclass leaves *two* entries of the same signature, which
QtWebKit then rejects as an ambiguous overloaded call.
"""

import functools
import sys

from classes import crash_handler

from PyQt5.QtCore import pyqtSlot

# Value handed back to JS when a slot raises, by declared return type.  Both
# spellings pyqtSlot accepts are covered: a Python type and a Qt type name.
_DEFAULTS = {
    None: None,
    bool: False,
    int: 0,
    float: 0.0,
    str: "",
    "void": None,
    "bool": False,
    "int": 0,
    "double": 0.0,
    "float": 0.0,
    "QString": "",
}
_FACTORIES = {
    list: list,
    dict: dict,
    "QStringList": list,
    "QVariantList": list,
    "QVariantMap": dict,
}


def _default_for(result):
    try:
        if result in _FACTORIES:
            return _FACTORIES[result]()
        return _DEFAULTS.get(result)
    except TypeError:       # unhashable declaration; nothing sensible to return
        return None


def guarded_slot(*types, **kwargs):
    """``pyqtSlot``, except a traceback is logged instead of escaping into Qt."""
    default = _default_for(kwargs.get("result"))

    def decorate(func):
        @functools.wraps(func, updated=())
        def wrapper(self, *args, **kw):
            try:
                return func(self, *args, **kw)
            except Exception:
                crash_handler.report(
                    *sys.exc_info(),
                    context="exception in bridge slot %s" % func.__qualname__,
                    show_dialog=False)
                return default
        wrapper.__zenvi_guarded__ = True
        return pyqtSlot(*types, **kwargs)(wrapper)

    return decorate
