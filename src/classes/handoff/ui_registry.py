"""Where handoff packages put their menu entries (pure Python; the menus are built by
``windows.handoff_menus.install_handoff_menus``).

A package registers at import time (its module is loaded by
``classes.handoff.plugins.load_plugins``)::

    from classes.handoff.ui_registry import register_export_action, register_send_action

    register_export_action("after_effects", "After Effects (.jsx)...", _export_dialog, order=10)
    register_send_action("after_effects", "After Effects", "aftereffects", _send, order=10)

* Export entries appear in File > Export Project, import entries in
  File > Import Project, each after a separator below the built-in ones,
  sorted by ``order`` then ``id``.
* Send entries appear in File > Send To; they are enabled only while
  their ``host_app`` (``aftereffects`` / ``premiere``) is connected
  (discovery refreshed off the GUI thread when the menu opens). A
  ``host_app`` of None means always enabled.
* ``handler(window)`` runs on the GUI thread when the user picks the entry.
  It must not block: ask for paths with dialogs there, then hand the work
  to ``classes.handoff.jobs.submit_job``.
* ``label`` is English; it is translated with the app's ``_tr`` when the
  menu is built. Pass a zero-argument callable instead to translate it
  yourself (``lambda: _("...")``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

Label = Union[str, Callable[[], str]]
Handler = Callable[[Any], None]

EXPORT, IMPORT, SEND = "export", "import", "send"
HOST_APPS = ("aftereffects", "premiere")


@dataclass(frozen=True)
class HandoffAction:
    id: str
    kind: str                 # export | import | send
    label: Label
    handler: Handler
    order: int = 100
    host_app: Optional[str] = None
    tooltip: str = ""

    def text(self, translate: Optional[Callable[[str], str]] = None) -> str:
        if callable(self.label):
            return str(self.label())
        return translate(self.label) if translate else self.label


_lock = threading.Lock()
_actions: Dict[str, Dict[str, HandoffAction]] = {EXPORT: {}, IMPORT: {}, SEND: {}}
_listeners: List[Callable[[], None]] = []


def _register(action: HandoffAction) -> HandoffAction:
    if not action.id or not isinstance(action.id, str):
        raise ValueError("a handoff menu action needs a string id")
    if not callable(action.handler):
        raise ValueError(f"handoff action {action.id!r}: handler must be callable")
    if action.kind == SEND and action.host_app not in HOST_APPS + (None,):
        raise ValueError(f"handoff action {action.id!r}: host_app must be one of {HOST_APPS} or None")
    with _lock:
        _actions[action.kind][action.id] = action
        listeners = list(_listeners)
    for listener in listeners:
        listener()
    return action


def register_export_action(id: str, label: Label, handler: Handler, order: int = 100, *,
                           tooltip: str = "") -> HandoffAction:
    """Add an entry to File > Export Project (replaces an earlier one with the same id)."""
    return _register(HandoffAction(id, EXPORT, label, handler, int(order), None, tooltip))


def register_import_action(id: str, label: Label, handler: Handler, order: int = 100, *,
                           tooltip: str = "") -> HandoffAction:
    """Add an entry to File > Import Project."""
    return _register(HandoffAction(id, IMPORT, label, handler, int(order), None, tooltip))


def register_send_action(id: str, label: Label, host_app: Optional[str], handler: Handler, order: int = 100, *,
                         tooltip: str = "") -> HandoffAction:
    """Add an entry to File > Send To, enabled while *host_app* is connected."""
    return _register(HandoffAction(id, SEND, label, handler, int(order), host_app, tooltip))


def unregister_action(id: str, kind: Optional[str] = None) -> None:
    with _lock:
        for k in ([kind] if kind else list(_actions)):
            _actions.get(k, {}).pop(id, None)


def actions(kind: str) -> List[HandoffAction]:
    """Registered actions of *kind* in menu order."""
    with _lock:
        found = list(_actions.get(kind, {}).values())
    return sorted(found, key=lambda a: (a.order, a.id))


def export_actions() -> List[HandoffAction]:
    return actions(EXPORT)


def import_actions() -> List[HandoffAction]:
    return actions(IMPORT)


def send_actions() -> List[HandoffAction]:
    return actions(SEND)


def add_listener(callback: Callable[[], None]) -> None:
    """Called (on the registering thread) whenever an action is registered -- the menus rebuild."""
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_listener(callback: Callable[[], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)
