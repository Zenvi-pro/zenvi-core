"""Failed / no-op tools must not leave undo history (Phase 3)."""

from unittest.mock import MagicMock, patch

from classes import tool_handlers
from classes.agent_tools.receipt import parse_receipt
from classes.update_queue import UpdateQueue, UpdatesRouter
from classes.updates import UpdateManager


class _Store:
    def __init__(self, clips=None, layers=None):
        self.clips = list(clips or [])
        self.layers = list(layers or [{"number": 1000000, "id": "t1"}])
        self.fps = {"num": 30, "den": 1}
        self.width = 1920
        self.height = 1080
        self.sample_rate = 48000

    def changed(self, action):
        if action.type == "load":
            return
        key = action.key or []
        if key and key[0] == "clips":
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
            return
        if len(key) == 1 and isinstance(key[0], str):
            name = key[0]
            if action.type == "update":
                old = getattr(self, name, None)
                if isinstance(old, dict):
                    action.set_old_values(dict(old))
                else:
                    action.set_old_values(old)
                setattr(self, name, action.values)

    def _find(self, key):
        for part in key:
            if isinstance(part, dict) and "id" in part:
                cid = part["id"]
                return next((c for c in self.clips if c.get("id") == cid), None)
        return None

    def get(self, name, default=None):
        if name == "clips":
            return self.clips
        if name == "layers":
            return self.layers
        if name == "fps":
            return self.fps
        if name in ("width", "height", "sample_rate"):
            return getattr(self, name, default)
        return default

    def generate_id(self):
        return "gen1"


def _app(clips=None):
    manager = UpdateManager()
    store = _Store(clips)
    manager.add_listener(store)
    app = MagicMock()
    app.project = store
    app.updates = UpdatesRouter(manager, UpdateQueue(manager))
    app.thread.return_value = object()
    app.window = MagicMock()
    return app, manager, store


def test_schema_refusal_creates_zero_history():
    app, manager, store = _app()
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None):
        raw = tool_handlers.execute_tool(
            "delete_from_timeline_tool",
            {"scope": "not-a-scope"},
        )
    receipt = parse_receipt(raw)
    assert receipt["status"] == "refused"
    assert receipt["undoSteps"] == 0
    assert manager.actionHistory == []


def test_project_setting_noop_zero_undo():
    app, manager, store = _app()
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None):
        raw = tool_handlers.execute_tool(
            "set_project_setting_tool",
            {"fps_num": 30, "fps_den": 1},
        )
    receipt = parse_receipt(raw)
    assert receipt["status"] == "unchanged"
    assert receipt["undoSteps"] == 0
    assert manager.actionHistory == []


def test_project_setting_change_is_one_undo_step():
    app, manager, store = _app()
    import classes.app as app_module
    import classes.updates as updates_module
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch.object(updates_module, "get_app", return_value=app), \
         patch.object(app_module, "get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None):
        raw = tool_handlers.execute_tool(
            "set_project_setting_tool",
            {"fps_num": 24, "fps_den": 1},
        )
        receipt = parse_receipt(raw)
        assert receipt is not None
        assert receipt["status"] == "applied", receipt
        assert store.fps == {"num": 24, "den": 1}
        tids = {a.transaction for a in manager.actionHistory}
        assert len(tids) == 1
        manager.undo()
        assert store.fps == {"num": 30, "den": 1}


def test_two_hundred_edits_fully_undo():
    app, manager, store = _app()
    import classes.app as app_module
    import classes.updates as updates_module
    with patch.object(tool_handlers, "_get_app", return_value=app), \
         patch.object(updates_module, "get_app", return_value=app), \
         patch.object(app_module, "get_app", return_value=app), \
         patch("classes.tool_handlers.QThread", None):
        for i in range(200):
            cid = f"c{i}"

            def _insert(cid=cid):
                app.updates.insert(
                    ["clips"],
                    {"id": cid, "position": 0, "start": 0, "end": 1, "layer": 0},
                )
                return f"inserted {cid}"

            with tool_handlers._transaction(app):
                _insert()
        assert len(store.clips) == 200
        for _ in range(200):
            manager.undo()
        assert store.clips == []
