"""A branch of the timeline the agent edits while the user keeps working (#426).

``start()`` copies the live project; while a thread is ``bound()`` to it,
``app.project`` reads the copy and ``app.updates`` writes go to it instead of
the live timeline (see UpdatesRouter).  ``apply_active()`` replays the
branch's actions on the live project as one undo step, skipping any action on
an object the user also changed since the copy was taken.
"""

import copy
import threading
import uuid
from contextlib import contextmanager


_tls = threading.local()
_active = None


def current():
    """The branch this thread is bound to, or None."""
    return getattr(_tls, "branch", None)


@contextmanager
def bound(branch):
    previous = current()
    _tls.branch = branch
    try:
        yield branch
    finally:
        _tls.branch = previous


def active():
    """The branch the current agent run edits, or None."""
    return _active


def start(live_store):
    """Branch the live project; reuse the pending branch if it holds edits."""
    global _active
    if _active is None or not _active.actionHistory:
        _active = Branch(live_store)
    return _active


def discard():
    global _active
    _active = None


def apply_active(updates):
    """Replay the pending branch on the live project; return the skipped actions."""
    global _active
    branch, _active = _active, None
    return branch.merge(updates) if branch else []


class RoutedProject:
    """``App.project``: the bound branch's copy on a bound thread, else the live store."""

    def __get__(self, app, owner=None):
        if app is None:
            return self
        branch = current()
        return branch.store if branch else app.__dict__.get("_live_project")

    def __set__(self, app, store):
        app.__dict__["_live_project"] = store


def _object_ref(key):
    """(collection, id) of the object an action key points into, else (top-level key, None)."""
    for part in key or []:
        if isinstance(part, dict) and "id" in part:
            return key[0], part["id"]
    return (key[0], None) if key else (None, None)


def _lookup(data, collection, object_id):
    value = data.get(collection)
    if object_id is None:
        return value
    return next((o for o in value or [] if isinstance(o, dict) and o.get("id") == object_id), None)


def _comparable(obj):
    """An object without its ``ui`` cache (waveforms land there in the background)."""
    if isinstance(obj, dict) and "ui" in obj:
        return {k: v for k, v in obj.items() if k != "ui"}
    return obj


def _refuse_file_io(*_args, **_kwargs):
    raise RuntimeError("The assistant is working on a draft copy of the timeline. "
                       "Apply or discard the draft before saving or opening a project.")


class Branch:
    def __init__(self, live_store):
        self.live = live_store
        self.base = copy.deepcopy(live_store._data)
        self.store = copy.copy(live_store)
        self.store._data = copy.deepcopy(self.base)
        # The copy shares the live file path: saving or opening from it would hit the user's project.
        self.store.save = self.store.load = _refuse_file_io
        self.actionHistory = []
        self.redoHistory = []
        self.version = 0
        self._lock = threading.RLock()  # tool calls arrive on parallel worker threads

    @property
    def data_version(self):
        # Never equal to the live manager's int, so QueryObject's cache is
        # dropped on every switch between branch and live reads.
        return ("branch", id(self), self.version)

    def record(self, action):
        with self._lock:
            self._apply(action)
            self.actionHistory.append(action)
            self.redoHistory.clear()

    def _apply(self, action):
        if action.type == "load":
            self.store._data = copy.deepcopy(action.values)
        else:
            if action.type == "delete":
                action.index = self.store._list_index(action.key)
            old = self.store._set(action.key, action.values, add=action.type == "insert",
                                  remove=action.type == "delete", index=getattr(action, "index", None))
            action.set_old_values(old)
        self.version += 1

    def _rebuild(self):
        from classes.updates import UpdateAction
        self.store._data = copy.deepcopy(self.base)
        for action in self.actionHistory:
            self._apply(UpdateAction(action.type, list(action.key or []),
                                     copy.deepcopy(action.values), transaction=action.transaction))

    def _move_tail(self, source, target):
        """Move the last transaction from *source* to *target*, in place (the undo tool holds both lists)."""
        tid = source[-1].transaction
        group = [a for a in source if a.transaction == tid]
        source[:] = [a for a in source if a.transaction != tid]
        target.extend(reversed(group))
        return group

    def undo(self):
        with self._lock:
            if self.actionHistory:
                self._move_tail(self.actionHistory, self.redoHistory)
                self._rebuild()

    def redo(self):
        with self._lock:
            if self.redoHistory:
                for action in reversed(self._move_tail(self.redoHistory, self.actionHistory)):
                    self._apply(action)

    def _conflicts(self, action):
        if action.type == "load":
            return self.live._data != self.base
        collection, object_id = _object_ref(action.key)
        if collection is None or (object_id is None and action.type == "insert"):
            return False
        return _comparable(_lookup(self.base, collection, object_id)) != _comparable(
            _lookup(self.live._data, collection, object_id))

    def merge(self, updates):
        """Replay on the live project as one undo step; return the skipped actions."""
        with self._lock, bound(None):
            previous = updates.transaction_id
            updates.transaction_id = str(uuid.uuid4())
            try:
                # Judge every action against the copy before replaying any of them.
                skipped = [a for a in self.actionHistory if self._conflicts(a)]
                for action in self.actionHistory:
                    if action in skipped:
                        continue
                    values = copy.deepcopy(action.values)
                    key = list(action.key or [])
                    if action.type == "insert":
                        updates.insert(key, values)
                    elif action.type == "update":
                        updates.update(key, values)
                    elif action.type == "delete":
                        updates.delete(key)
                    elif action.type == "load":
                        updates.load(values, reset_history=False)
            finally:
                updates.transaction_id = previous
        return skipped
